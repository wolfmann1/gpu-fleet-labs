"""
train_ddp.py - a small distributed training job for GPU-fleet labs.

Trains a convolutional classifier on synthetic image data with PyTorch
DistributedDataParallel (DDP). The model and data are deliberately simple:
the point is to exercise the platform (scheduling, NCCL, checkpoints,
failure and resume, metrics), not to learn anything useful.

Run under torchrun, which sets RANK, LOCAL_RANK, WORLD_SIZE, MASTER_ADDR
and MASTER_PORT for each process:

    # one node, all local GPUs
    torchrun --nproc-per-node=gpu train_ddp.py --steps 2000

    # two nodes, one GPU each (run on both; node-rank 0 and 1)
    torchrun --nnodes=2 --nproc-per-node=1 --node-rank=$RANK_OF_THIS_NODE \
             --master-addr=$MASTER --master-port=29500 train_ddp.py

Options worth knowing:
    --ckpt-dir DIR      save a checkpoint every --ckpt-every steps and resume
                        from the newest one at start-up
    --data-delay-ms N   sleep N ms per batch in the data loader, to imitate
                        slow storage and starve the GPU
    --batch-size N      per-GPU batch size; raise it to fill GPU memory
"""

import argparse
import os
import time
from datetime import timedelta

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, Dataset
from torch.utils.data.distributed import DistributedSampler


class SyntheticImages(Dataset):
    """Random 3x64x64 images with random labels. Deterministic per index."""

    def __init__(self, length: int, num_classes: int, delay_ms: int):
        self.length = length
        self.num_classes = num_classes
        self.delay_s = delay_ms / 1000.0

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        if self.delay_s:
            time.sleep(self.delay_s)  # imitate slow reads from storage
        g = torch.Generator().manual_seed(idx)
        image = torch.randn(3, 64, 64, generator=g)
        label = int(torch.randint(0, self.num_classes, (1,), generator=g))
        return image, label


def build_model(num_classes: int) -> nn.Module:
    def block(cin, cout):
        return nn.Sequential(
            nn.Conv2d(cin, cout, 3, padding=1),
            nn.BatchNorm2d(cout),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
        )

    return nn.Sequential(
        block(3, 64),
        block(64, 128),
        block(128, 256),
        block(256, 512),
        nn.AdaptiveAvgPool2d(1),
        nn.Flatten(),
        nn.Linear(512, num_classes),
    )


def latest_checkpoint(ckpt_dir: str):
    if not ckpt_dir or not os.path.isdir(ckpt_dir):
        return None
    files = [f for f in os.listdir(ckpt_dir) if f.startswith("step-") and f.endswith(".pt")]
    if not files:
        return None
    files.sort(key=lambda f: int(f[len("step-"):-len(".pt")]))
    return os.path.join(ckpt_dir, files[-1])


def log(rank: int, msg: str):
    if rank == 0:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--steps", type=int, default=2000)
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--lr", type=float, default=0.05)
    p.add_argument("--num-classes", type=int, default=100)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--data-delay-ms", type=int, default=0)
    p.add_argument("--ckpt-dir", type=str, default="")
    p.add_argument("--ckpt-every", type=int, default=200)
    p.add_argument("--log-every", type=int, default=50)
    p.add_argument("--nccl-timeout-s", type=int, default=120)
    args = p.parse_args()

    # torchrun provides these; the NCCL backend uses MASTER_ADDR/PORT to rendezvous.
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    dist.init_process_group(
        backend="nccl",
        timeout=timedelta(seconds=args.nccl_timeout_s),  # how long a hung collective waits before failing
    )
    rank = dist.get_rank()
    world = dist.get_world_size()
    device = torch.device("cuda", local_rank)

    log(rank, f"world size {world}; rank 0 on {torch.cuda.get_device_name(device)}")

    model = build_model(args.num_classes).to(device)
    model = DDP(model, device_ids=[local_rank])
    opt = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=0.9)
    loss_fn = nn.CrossEntropyLoss()

    start_step = 0
    ckpt = latest_checkpoint(args.ckpt_dir)
    if ckpt:
        state = torch.load(ckpt, map_location=device)
        model.module.load_state_dict(state["model"])
        opt.load_state_dict(state["opt"])
        start_step = state["step"]
        log(rank, f"resumed from {ckpt} at step {start_step}")
    elif args.ckpt_dir:
        log(rank, f"no checkpoint in {args.ckpt_dir}; starting from step 0")

    dataset = SyntheticImages(
        length=args.steps * args.batch_size * world,
        num_classes=args.num_classes,
        delay_ms=args.data_delay_ms,
    )
    sampler = DistributedSampler(dataset, num_replicas=world, rank=rank, shuffle=False)
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        sampler=sampler,
        num_workers=args.workers,
        pin_memory=True,
        persistent_workers=args.workers > 0,
    )

    model.train()
    step = start_step
    t0 = time.time()
    images_since_log = 0
    batches = iter(loader)  # synthetic data, so a resumed run simply draws fresh batches

    while step < args.steps:
        try:
            images, labels = next(batches)
        except StopIteration:
            break
        images = images.to(device, non_blocking=True)
        labels = labels.to(device, non_blocking=True)

        opt.zero_grad(set_to_none=True)
        loss = loss_fn(model(images), labels)
        loss.backward()  # DDP all-reduces gradients across ranks here
        opt.step()
        step += 1
        images_since_log += images.size(0) * world

        if step % args.log_every == 0:
            torch.cuda.synchronize()
            elapsed = time.time() - t0
            mem_gb = torch.cuda.max_memory_allocated(device) / 1e9
            log(
                rank,
                f"step {step}/{args.steps} loss {loss.item():.3f} "
                f"{images_since_log / elapsed:,.0f} img/s total "
                f"peak mem {mem_gb:.2f} GB/GPU",
            )
            t0 = time.time()
            images_since_log = 0

        if args.ckpt_dir and step % args.ckpt_every == 0:
            if rank == 0:
                os.makedirs(args.ckpt_dir, exist_ok=True)
                path = os.path.join(args.ckpt_dir, f"step-{step}.pt")
                tmp = path + ".tmp"
                torch.save(
                    {"model": model.module.state_dict(), "opt": opt.state_dict(), "step": step},
                    tmp,
                )
                os.replace(tmp, path)  # atomic rename: a crash never leaves a half-written checkpoint
                log(rank, f"checkpoint {path}")
            dist.barrier()

    log(rank, f"finished at step {step}")
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
