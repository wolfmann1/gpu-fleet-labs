"""
preflight.py - GPU and interconnect check to run before a training job.

Run under torchrun with the same shape as the real job:

    torchrun --nproc-per-node=gpu preflight.py
    torchrun --nnodes=2 --nproc-per-node=1 --node-rank=... \
             --master-addr=... --master-port=29500 preflight.py

Each rank checks its own GPU; then all ranks run an NCCL all-reduce
together. Exit code 0 means every check passed on every rank. Any
failure prints the reason and exits non-zero, so a scheduler, init
container or wrapper script can refuse to start the real job.

Checks:
  1. nvidia-smi health: uncorrected ECC errors, pending row remaps,
     temperature, and active clock-throttle reasons (fields a GeForce
     card doesn't report are skipped and noted).
  2. Compute: a matrix multiply on the GPU, verified against the CPU.
  3. Collective: an NCCL all-reduce across all ranks, verified for
     correctness and timed to estimate bus bandwidth.
"""

import argparse
import os
import socket
import subprocess
import sys
import time
from datetime import timedelta

import torch
import torch.distributed as dist

QUERY_FIELDS = [
    "index",
    "name",
    "pci.bus_id",
    "temperature.gpu",
    "ecc.errors.uncorrected.volatile.total",
    "remapped_rows.pending",
    "remapped_rows.failure",
    "clocks_throttle_reasons.hw_slowdown",
    "clocks_throttle_reasons.hw_thermal_slowdown",
    "clocks_throttle_reasons.sw_thermal_slowdown",
]

NOT_REPORTED = {"[N/A]", "N/A", "[Not Supported]", "Not Supported", ""}


def smi_query(gpu_index: int) -> dict:
    """Query one GPU via nvidia-smi. Returns field -> string value."""
    values = {}
    for field in QUERY_FIELDS:
        # One field per call: an unsupported field fails the whole query on some drivers.
        out = subprocess.run(
            ["nvidia-smi", f"--id={gpu_index}", f"--query-gpu={field}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0 and field.startswith("clocks_throttle_reasons."):
            # Newer drivers call these clocks_event_reasons; try the new name.
            alt = field.replace("clocks_throttle_reasons.", "clocks_event_reasons.")
            out = subprocess.run(
                ["nvidia-smi", f"--id={gpu_index}", f"--query-gpu={alt}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
            )
        values[field] = out.stdout.strip() if out.returncode == 0 else "[Not Supported]"
    return values


def physical_gpu_index(local_rank: int) -> int:
    """Map this rank's CUDA device to nvidia-smi's index, honouring CUDA_VISIBLE_DEVICES."""
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible:
        ids = [v.strip() for v in visible.split(",") if v.strip()]
        if local_rank < len(ids) and ids[local_rank].isdigit():
            return int(ids[local_rank])
    return local_rank


def check_health(local_rank: int, max_temp: int) -> list:
    problems, skipped = [], []
    try:
        v = smi_query(physical_gpu_index(local_rank))
    except FileNotFoundError:
        return ["nvidia-smi not found in the container or on the host"]

    def num(field):
        raw = v.get(field, "")
        if raw in NOT_REPORTED:
            skipped.append(field)
            return None
        try:
            return float(raw)
        except ValueError:
            return raw  # e.g. "Active" / "Not Active" for throttle reasons

    ecc = num("ecc.errors.uncorrected.volatile.total")
    if isinstance(ecc, float) and ecc > 0:
        problems.append(f"{int(ecc)} uncorrected ECC errors since last reset")

    pending = num("remapped_rows.pending")
    if pending not in (None, 0.0, "No"):
        problems.append(f"row remap pending ({pending}); GPU needs a reset before use")

    failed = num("remapped_rows.failure")
    if failed not in (None, 0.0, "No"):
        problems.append(f"row remap failure ({failed}); GPU should be replaced")

    temp = num("temperature.gpu")
    if isinstance(temp, float) and temp > max_temp:
        problems.append(f"temperature {temp:.0f} C above limit {max_temp} C")

    for field in QUERY_FIELDS[-3:]:
        state = num(field)
        if isinstance(state, str) and state.lower() == "active":
            problems.append(f"{field.split('.')[-1]} throttling active")

    if skipped:
        print(f"  gpu {local_rank}: not reported by this GPU/driver: {', '.join(skipped)}", flush=True)
    print(f"  gpu {local_rank}: {v.get('name')} {v.get('pci.bus_id')} temp {v.get('temperature.gpu')} C", flush=True)
    return problems


def check_compute(device) -> list:
    torch.manual_seed(0)
    a = torch.randn(2048, 2048)
    b = torch.randn(2048, 2048)
    expected = a @ b
    got = (a.to(device) @ b.to(device)).cpu()
    err = (got - expected).abs().max().item()
    # TF32/FP32 differences are expected to be small; large error suggests bad hardware.
    return [] if err < 1.0 else [f"matmul mismatch vs CPU, max abs error {err:.3f}"]


def check_allreduce(device, size_mb: int, iters: int) -> tuple:
    world = dist.get_world_size()
    rank = dist.get_rank()
    numel = size_mb * 1024 * 1024 // 4  # float32
    buf = torch.full((numel,), float(rank + 1), device=device)
    expected = world * (world + 1) / 2  # sum of 1..world

    dist.all_reduce(buf)  # warm-up; also establishes NCCL communicators
    torch.cuda.synchronize()
    if not torch.allclose(buf[:1024], torch.full((1024,), expected, device=device)):
        return [f"all-reduce returned wrong values (expected {expected})"], 0.0

    buf.fill_(1.0)
    torch.cuda.synchronize()
    t0 = time.time()
    for _ in range(iters):
        dist.all_reduce(buf)
    torch.cuda.synchronize()
    elapsed = (time.time() - t0) / iters

    # Bus bandwidth as nccl-tests reports it: algbw * 2(n-1)/n.
    algbw = (numel * 4) / elapsed / 1e9
    busbw = algbw * 2 * (world - 1) / world if world > 1 else algbw
    return [], busbw


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-temp", type=int, default=85)
    p.add_argument("--allreduce-mb", type=int, default=256)
    p.add_argument("--iters", type=int, default=20)
    p.add_argument("--min-busbw-gbps", type=float, default=0.0,
                   help="fail if measured bus bandwidth (GB/s) is below this; 0 disables")
    p.add_argument("--timeout-s", type=int, default=60)
    args = p.parse_args()

    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    dist.init_process_group(backend="nccl", timeout=timedelta(seconds=args.timeout_s))
    rank, world = dist.get_rank(), dist.get_world_size()
    host = socket.gethostname()

    problems = check_health(local_rank, args.max_temp)
    problems += check_compute(device)

    # Every rank must reach the collective even if its local checks failed,
    # otherwise healthy ranks would hang waiting for it.
    ar_problems, busbw = check_allreduce(device, args.allreduce_mb, args.iters)
    problems += ar_problems

    if rank == 0 and busbw:
        print(f"all-reduce {args.allreduce_mb} MB across {world} ranks: bus bandwidth {busbw:.1f} GB/s", flush=True)
    if args.min_busbw_gbps and busbw and busbw < args.min_busbw_gbps:
        problems.append(f"bus bandwidth {busbw:.1f} GB/s below minimum {args.min_busbw_gbps}")

    # Gather results so rank 0 can print one verdict for the whole allocation.
    local_ok = torch.tensor([0 if problems else 1], device=device)
    dist.all_reduce(local_ok, op=dist.ReduceOp.MIN)
    for msg in problems:
        print(f"FAIL rank {rank} ({host}, local gpu {local_rank}): {msg}", flush=True)

    all_ok = bool(local_ok.item())
    if rank == 0:
        print("PREFLIGHT PASSED" if all_ok else "PREFLIGHT FAILED", flush=True)
    dist.destroy_process_group()
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
