import numpy as np
import os
import torch
import torch.nn as nn
from argparse import ArgumentParser
from torch.cuda import device_count
from torch.multiprocessing import spawn
from torch.nn.parallel import DistributedDataParallel
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from dataset import create_dataloader
from model import NUWave
from params import params


def _nested_map(struct, map_fn):
    if isinstance(struct, tuple):
        return tuple(_nested_map(x, map_fn) for x in struct)
    if isinstance(struct, list):
        return [_nested_map(x, map_fn) for x in struct]
    if isinstance(struct, dict):
        return {k: _nested_map(v, map_fn) for k, v in struct.items()}
    return map_fn(struct)


class NUWaveLearner:
    def __init__(self, model_dir, model, dataset, optimizer, params, *args, **kwargs):
        os.makedirs(model_dir, exist_ok=True)
        self.model_dir = model_dir
        self.model = model
        self.dataset = dataset
        self.optimizer = optimizer
        self.params = params
        self.autocast = torch.cuda.amp.autocast(enabled=kwargs.get("fp16", False))
        self.scaler = torch.cuda.amp.GradScaler(enabled=kwargs.get("fp16", False))
        self.step = 0
        self.is_master = True

        beta = np.array(self.params.noise_schedule)
        noise_level = np.cumprod(1 - beta)
        noise_level = np.concatenate([[1.0], noise_level], axis=0)
        self.noise_level = torch.tensor(noise_level.astype(np.float32))
        self.loss_fn = nn.L1Loss()
        self.summary_writer = None

    def state_dict(self):
        if hasattr(self.model, "module") and isinstance(self.model.module, nn.Module):
            model_state = self.model.module.state_dict()
        else:
            model_state = self.model.state_dict()
        return {
            "step": self.step,
            "model": {
                k: v.cpu() if isinstance(v, torch.Tensor) else v
                for k, v in model_state.items()
            },
            "optimizer": {
                k: v.cpu() if isinstance(v, torch.Tensor) else v
                for k, v in self.optimizer.state_dict().items()
            },
            "params": dict(self.params),
            "scaler": self.scaler.state_dict(),
        }

    def load_state_dict(self, state_dict):
        if hasattr(self.model, "module") and isinstance(self.model.module, nn.Module):
            self.model.module.load_state_dict(state_dict["model"])
        else:
            self.model.load_state_dict(state_dict["model"])
        self.optimizer.load_state_dict(state_dict["optimizer"])
        self.scaler.load_state_dict(state_dict["scaler"])
        self.step = state_dict["step"]

    def save_to_checkpoint(self, filename="weights"):
        save_basename = f"{filename}-{self.step}.pt"
        save_name = f"{self.model_dir}/{save_basename}"
        link_name = f"{self.model_dir}/{filename}.pt"
        torch.save(self.state_dict(), save_name)
        if os.name == "nt":
            torch.save(self.state_dict(), link_name)
        else:
            if os.path.islink(link_name):
                os.unlink(link_name)
            os.symlink(save_basename, link_name)

    def restore_from_checkpoint(self, filename="weights"):
        try:
            checkpoint = torch.load(f"{self.model_dir}/{filename}.pt")
            self.load_state_dict(checkpoint)
            return True
        except FileNotFoundError:
            return False

    def train(self, max_steps=None):
        device = next(self.model.parameters()).device
        while True:
            for features in (
                tqdm(self.dataset, desc=f"Epoch {self.step // len(self.dataset)}")
                if self.is_master
                else self.dataset
            ):
                if max_steps is not None and self.step >= max_steps:
                    return
                features = _nested_map(
                    features,
                    lambda x: x.to(device) if isinstance(x, torch.Tensor) else x,
                )
                loss = self.train_step(features)
                if torch.isnan(loss).any():
                    raise RuntimeError(f"Detected NaN loss at step {self.step}.")
                if self.is_master:
                    if self.step % 50 == 0:
                        self._write_summary(self.step, features, loss)
                    if self.step % len(self.dataset) == 0:
                        self.save_to_checkpoint()
                self.step += 1

    def train_step(self, features):
        for param in self.model.parameters():
            param.grad = None

        lr_audio = features["lr_audio"]
        audio = features["audio"]

        N, T = audio.shape
        S = 1000
        device = audio.device
        self.noise_level = self.noise_level.to(device)

        with self.autocast:

            s = torch.randint(1, S + 1, [N], device=audio.device)
            l_a, l_b = self.noise_level[s - 1], self.noise_level[s]
            noise_scale = l_a + torch.rand(N, device=audio.device) * (l_b - l_a)
            noise_scale = noise_scale.unsqueeze(1)
            noise = torch.randn_like(audio)

            noisy_audio = noise_scale * audio + (1.0 - noise_scale ** 2) ** 0.5 * noise
            predicted = self.model(noisy_audio, lr_audio, noise_scale.squeeze(1))
            loss = self.loss_fn(noise, predicted.squeeze(1))

        self.scaler.scale(loss).backward()
        self.scaler.unscale_(self.optimizer)
        self.grad_norm = nn.utils.clip_grad_norm_(
            self.model.parameters(), self.params.max_grad_norm or 1e9
        )
        self.scaler.step(self.optimizer)
        self.scaler.update()
        return loss

    def _write_summary(self, step, features, loss):
        writer = self.summary_writer or SummaryWriter(self.model_dir, purge_step=step)
        writer.add_audio(
            "feature/audio",
            features["audio"][0],
            step,
            sample_rate=self.params.new_sample_rate,
        )
        writer.add_audio(
            "feature/lr_audio",
            features["lr_audio"][0],
            step,
            sample_rate=self.params.sample_rate,
        )
        writer.add_scalar("train/loss", loss, step)
        writer.add_scalar("train/grad_norm", self.grad_norm, step)
        writer.flush()
        self.summary_writer = writer


def _train_impl(replica_id, model, dataset, args, params):
    torch.backends.cudnn.benchmark = True
    opt = torch.optim.Adam(model.parameters(), lr=params.learning_rate)

    learner = NUWaveLearner(args.model_dir, model, dataset, opt, params, fp16=args.fp16)
    learner.is_master = replica_id == 0
    learner.restore_from_checkpoint()
    learner.train(max_steps=args.max_steps)


def train(args, params):
    dataset = create_dataloader(
        params,
        True,
    )
    model = NUWave(params).cuda()
    _train_impl(0, model, dataset, args, params)


def train_distributed(replica_id, replica_count, port, args, params):
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = str(port)
    torch.distributed.init_process_group(
        "nccl", rank=replica_id, world_size=replica_count
    )

    device = torch.device("cuda", replica_id)
    torch.cuda.set_device(device)
    model = NUWave(params).to(device)
    model = DistributedDataParallel(model, device_ids=[replica_id])
    _train_impl(
        replica_id,
        model,
        create_dataloader(params, True, is_distributed=True),
        args,
        params,
    )


def _get_free_port():
    import socketserver

    with socketserver.TCPServer(("localhost", 0), None) as s:
        return s.server_address[1]


def main(args):
    replica_count = device_count()
    if replica_count > 1:
        if params.batch_size % replica_count != 0:
            raise ValueError(
                f"Batch size {params.batch_size} is not evenly divisble by # GPUs {replica_count}."
            )
        params.batch_size = params.batch_size // replica_count
        port = _get_free_port()
        spawn(
            train_distributed,
            args=(replica_count, port, args, params),
            nprocs=replica_count,
            join=True,
        )
    else:
        train(args, params)


if __name__ == "__main__":
    parser = ArgumentParser(description="train (or resume training) a DiffWave model")
    parser.add_argument(
        "model_dir",
        help="directory in which to store model checkpoints and training logs",
    )
    # parser.add_argument('data_dirs', nargs='+', help='space separated list of directories from which to read .wav files for training')
    parser.add_argument(
        "--max_steps", default=None, type=int, help="maximum number of training steps"
    )
    parser.add_argument(
        "--fp16",
        action="store_true",
        default=False,
        help="use 16-bit floating point operations for training",
    )
    main(parser.parse_args())
