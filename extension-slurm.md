# Extension — an evening of Slurm

[Labs index](README.md) · Previous: [Stage 2 — Kubernetes GPU node](stage-2-k8s-gpu-node.md) · Next: [Stage 3 — EKS GPU cluster](stage-3-eks-gpu-cluster.md)

## Why this lab

Some researchers on the [reference fleet](scenario.md) ask for Slurm. Before deciding whether to give it to them, you need to know what they'd get: `sbatch` scripts, GPU allocation as generic resources, backfill, and partition or QOS-based priority and preemption. One evening on a single node covers the parts that matter for that conversation.

By the end you will have:

- a one-node Slurm cluster on the GPU node with both GPUs as schedulable resources;
- run batch jobs and seen how Slurm hands out GPUs;
- watched **backfill** start a short job early, which neither dstack nor Kueue does;
- preempted a job through a higher-priority partition.

## Where things run

Everything in this lab runs **on the GPU node**, over SSH from the laptop, except the two commands in the first step of Build that free the GPUs from dstack and k3s, which run on the laptop.

## Concepts

| Slurm term | Meaning | Nearest equivalent you've used |
|---|---|---|
| `slurmctld` | The controller: queue, scheduling, state | dstack server / Kueue controller |
| `slurmd` | Agent on each compute node; launches job steps | dstack shim / kubelet |
| `munge` | Shared-key authentication between daemons | — |
| Node | A machine and its CPUs, memory and GRES | Node |
| GRES | Generic resource, e.g. `gpu:rtx3070:2` | `nvidia.com/gpu` |
| Partition | A named queue over a set of nodes, with limits and a priority tier | ClusterQueue, loosely |
| QOS | Quality of service: priority boost, limits, preemption rules; needs the accounting database | WorkloadPriorityClass plus limits |
| `sbatch` / `srun` | Submit a batch script / launch a step (or an interactive job) | `dstack apply` / `kubectl create` |
| Backfill | Start lower-priority jobs early if they will finish before a waiting higher-priority job could start | — |
| Fair share | Priority adjusted by each account's recent usage relative to its share; needs accounting | Kueue fair sharing |

Slurm has no containers by default. Jobs run as your user directly on the host, with `CUDA_VISIBLE_DEVICES` set to the GPUs allocated. HPC sites add containers through plugins (Pyxis/Enroot) or Apptainer. That difference is often the real reason researchers from HPC want Slurm: their workflow is a Python environment and an `sbatch` script, with no image to build.

Backfill depends on every job declaring a time limit (`--time`). Slurm can only fit a short job into a gap if it knows the job is short. A culture of honest time limits is part of running Slurm well.

---

## Build

First free the GPUs from the other two schedulers, so three aren't competing for the same cards.

**On the laptop (WSL2):**

```bash
dstack fleet delete home                     # dstack stops using the GPU node
for ns in team-a team-b; do kubectl -n $ns delete jobs --all; done   # k3s training jobs off the GPUs
```

dstack no longer schedules work onto the GPU node, and the `team-a` and `team-b` training jobs are gone from k3s, so both GPUs are free. The rest of this lab runs on the GPU node; SSH in (`ssh chris@192.168.1.50`) and stay there.

### B1. Packages

Install Slurm, the munge authentication service and the Python tooling, then start munge and confirm it can encode and decode a credential. `slurmd -C` reports this machine's hardware in the format `slurm.conf` expects.

**On the GPU node:**

```bash
sudo apt install -y slurm-wlm munge python3-venv python3-pip    # prerequisites §1.3
sudo systemctl enable --now munge
munge -n | unmunge | head -3        # STATUS: Success (0)
slurmd -C                            # prints this machine's NodeName line
```

Copy the `CPUs=`, `Boards=`, `SocketsPerBoard=`, `CoresPerSocket=`, `ThreadsPerCore=` and `RealMemory=` values from `slurmd -C`; they go into the node definition below.

### B2. Configuration

Tell Slurm which device files hold the GPUs. This file names the node, the resource type and the two NVIDIA devices. Create the file with the contents below; it's under `/etc`, so open it with `sudo nano /etc/slurm/gres.conf`.

**File on the GPU node:** `/etc/slurm/gres.conf`

```
NodeName=gpu-node Name=gpu Type=rtx3070 File=/dev/nvidia[0-1]
```

`slurmd` reads this file at startup and maps each `gpu:rtx3070` unit to one device file. (With one card: `File=/dev/nvidia0`, and `gpu:rtx3070:1` below.)

The main configuration defines the cluster, the scheduler, the node and two partitions. Replace the node's hardware values with the ones you copied from `slurmd -C`. The package doesn't install one, so create it with the contents below using `sudo nano /etc/slurm/slurm.conf`.

**File on the GPU node:** `/etc/slurm/slurm.conf`

```
ClusterName=home
SlurmctldHost=gpu-node
AuthType=auth/munge
SlurmUser=slurm
StateSaveLocation=/var/lib/slurm/slurmctld
SlurmdSpoolDir=/var/lib/slurm/slurmd
SlurmctldPidFile=/run/slurmctld.pid
SlurmdPidFile=/run/slurmd.pid
SlurmctldLogFile=/var/log/slurm/slurmctld.log
SlurmdLogFile=/var/log/slurm/slurmd.log
ProctrackType=proctrack/linuxproc
TaskPlugin=task/none
ReturnToService=2

# Scheduling
SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
GresTypes=gpu

# Priority and preemption without an accounting database:
# a higher partition PriorityTier preempts jobs in lower tiers.
PriorityType=priority/multifactor
PriorityWeightAge=1000
PriorityWeightJobSize=1000
PreemptType=preempt/partition_prio
PreemptMode=REQUEUE

AccountingStorageType=accounting_storage/none
JobCompType=jobcomp/none

# Paste the values from `slurmd -C`; keep Gres
NodeName=gpu-node CPUs=16 Boards=1 SocketsPerBoard=1 CoresPerSocket=8 ThreadsPerCore=2 RealMemory=31000 Gres=gpu:rtx3070:2 State=UNKNOWN

PartitionName=batch  Nodes=gpu-node Default=YES MaxTime=INFINITE PriorityTier=1  State=UP
PartitionName=urgent Nodes=gpu-node Default=NO  MaxTime=INFINITE PriorityTier=10 State=UP
```

| Line | Why |
|---|---|
| `SelectType=select/cons_tres` | Allocates individual cores, memory and GPUs, so several jobs can share a node |
| `SchedulerType=sched/backfill` | Enables backfill (exercise S3) |
| `GresTypes=gpu` + `Gres=` + `gres.conf` | Declares the GPUs and which device files they are |
| `PreemptType=preempt/partition_prio` | Jobs in a higher-tier partition can preempt lower-tier jobs on the same node |
| `PreemptMode=REQUEUE` | Preempted jobs go back in the queue (and should resume from checkpoint) |
| `ProctrackType=proctrack/linuxproc`, `TaskPlugin=task/none` | Simplest process tracking; production uses cgroups, which also stop a job from touching GPUs it wasn't allocated |

Create the state and log directories, give them to the `slurm` user, then start the controller and the node agent.

**On the GPU node:**

```bash
sudo mkdir -p /var/lib/slurm/slurmctld /var/lib/slurm/slurmd /var/log/slurm
sudo chown -R slurm:slurm /var/lib/slurm /var/log/slurm
sudo systemctl enable --now slurmctld slurmd
sinfo
scontrol show node gpu-node | grep -E 'Gres|State'
```

**Check, on the GPU node:** `sinfo` shows both partitions `idle`; the node reports `Gres=gpu:rtx3070:2`. If the node is `drain` or `down`, `scontrol show node` gives the reason (usually a CPU or memory value that doesn't match `slurmd -C`).

### B3. A Python environment for jobs

Slurm jobs use what's on the host, so give your user a PyTorch environment. The second command puts the training and preflight scripts in `~/lab`.

**On the GPU node:**

```bash
python3 -m venv ~/venv && ~/venv/bin/pip install torch
mkdir -p ~/lab && cp train_ddp.py preflight.py ~/lab/     # copy the scripts over first
```

`~/venv` now holds PyTorch, and `~/lab` holds `train_ddp.py` and `preflight.py`.

The batch script requests one GPU, four CPUs, 12 GB of memory and a 30-minute limit, then runs the training script under `torchrun`. Create it in `~/lab` with the contents below.

**File on the GPU node:** `~/lab/train.sbatch`

```bash
#!/bin/bash
#SBATCH --job-name=train
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=12G
#SBATCH --time=00:30:00
#SBATCH --output=%x-%j.out
#SBATCH --requeue

source ~/venv/bin/activate
echo "Allocated GPUs: $CUDA_VISIBLE_DEVICES on $(hostname)"
srun torchrun --nproc-per-node=${SLURM_GPUS_ON_NODE:-1} ~/lab/train_ddp.py \
     --steps 3000 --ckpt-dir ~/lab/ckpt/$SLURM_JOB_NAME
```

`sbatch` reads the `#SBATCH` lines at submission; options given on the command line override them, which the exercises use. `--requeue` lets Slurm return the job to the queue after preemption, and `--ckpt-dir` gives each job name its own checkpoint directory.

---

## Exercises

All exercises run **on the GPU node**, in `~/lab`.

### S1. First job

Submit the batch script, look at the queue and inspect what Slurm allocated. Replace `<id>` with the job ID that `sbatch` prints.

**On the GPU node:**

```bash
cd ~/lab && sbatch train.sbatch
squeue
scontrol show job <id> | grep -E 'TRES|Gres|TimeLimit'
tail -f train-<id>.out
```

**What to notice:** the first line of output shows `CUDA_VISIBLE_DEVICES`. Slurm gave the job one GPU by setting that variable. With `task/none` nothing stops the job ignoring it; production clusters use cgroups to enforce it.

### S2. Queueing on GPUs

Submit three copies (`sbatch --job-name=t1 train.sbatch`, `t2`, `t3`). `squeue` shows two running and one `PD` with reason `(Resources)`.

### S3. Backfill

This is the feature to see tonight. It needs both cards.

1. Submit a long 1-GPU job: `sbatch --job-name=long --time=00:20:00 train.sbatch` (raise `--steps` so it really runs ~20 minutes).
2. Submit a 2-GPU job that must wait for `long`: `sbatch --job-name=big --gres=gpu:2 --time=00:20:00 train.sbatch`. It pends with `(Resources)`. `squeue --start` shows its expected start time: when `long` ends.
3. Submit a short 1-GPU job: `sbatch --job-name=short --gres=gpu:1 --time=00:05:00 --wrap "sleep 240"`.

**What to notice:** `short` starts immediately on the free GPU even though `big` was ahead of it, because it will finish before `long` does, so it can't delay `big`. That's backfill. Now repeat step 3 with `--time=00:30:00`: this time `short` waits, because a 30-minute job would delay `big`.

In stage 2B, dstack's queue simply waited in priority order; Kueue does the same. On a 256-GPU fleet where a 32-node job is waiting for the last few nodes to free up, backfill is the difference between those nodes idling and doing useful short work.

### S4. Preemption through a partition

1. Fill both GPUs with `batch` jobs (two copies of `train.sbatch`).
2. Submit `sbatch --partition=urgent --job-name=deadline train.sbatch`.

**What to notice:** one `batch` job is requeued (`squeue` shows it pending again, and its output file records the requeue); `deadline` starts. When `deadline` ends, the requeued job restarts and resumes from its checkpoint. Compare with dstack (no preemption) and Kueue (preemption by WorkloadPriorityClass).

### S5. What researchers would get

Fill in from your own runs:

| Capability | dstack | Kueue on EKS | Slurm |
|---|---|---|---|
| Submit a job | YAML + `dstack apply` | Job YAML + `kubectl` (or a front end) | `sbatch` script |
| Environment | Container image | Container image | Host environment (containers via plugins) |
| Per-team quota | | | Partitions, QOS limits, accounts (with accounting) |
| Borrowing idle quota | | | |
| Preemption | | | |
| Backfill | | | |
| Fair share | | | With accounting (`slurmdbd`) |
| Interactive dev | Dev environments | Notebooks / exec | `srun --pty bash` |

---

## Going further

- **Accounting and fair share:** install `slurmdbd` with MariaDB, set `AccountingStorageType=accounting_storage/slurmdbd`, create accounts per team with `sacctmgr`, and add `PriorityWeightFairshare`. `sshare` and `sprio` then show how recent usage lowers a team's priority. This is what "fair share" means to an HPC researcher.
- **QOS-based preemption:** with accounting, `PreemptType=preempt/qos` lets a `deadline` QOS preempt `normal` jobs across partitions.
- **Slurm on Kubernetes:** Slinky (SchedMD's operator) runs Slurm's daemons as pods, which is one way a Kubernetes-based fleet could offer `sbatch` without a separate cluster. Read its overview with the E8 result from stage 2 in mind.

## Record

In `gpu-fleet-lab/notes/slurm.md`:

1. What did S3 show that dstack and Kueue don't do? How much would it matter on 32 nodes?
2. What would the fleet team need to run to give researchers Slurm with fair share (daemons, database, identity, storage)?
3. Who are the researchers asking for Slurm likely to be, and what do they actually want from it? Draft two questions to ask them.

## Clean up

Stop both Slurm daemons and disable them so they don't start at boot.

**On the GPU node:**

```bash
sudo systemctl disable --now slurmctld slurmd
```

Leave the packages; they're harmless when stopped.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Node `down` or `drain` | `slurm.conf` values don't match hardware | Compare with `slurmd -C`; then `scontrol update nodename=gpu-node state=resume` |
| `Invalid generic resource (gres) specification` | `GresTypes`, node `Gres=` and `gres.conf` disagree | Type and count must match in all three |
| `munge` errors in logs | Service not running or key permissions | `systemctl status munge`; key is `/etc/munge/munge.key`, owned by `munge`, mode 400 |
| Job pending with `(PartitionConfig)` | Asked for more than the partition allows | Check `scontrol show partition` |
| Preempted job doesn't return | Job not requeueable | Keep `#SBATCH --requeue` |

## References

- [Slurm — Quick start administrator guide](https://slurm.schedmd.com/quickstart_admin.html)
- [Slurm — Generic resource (GRES) scheduling](https://slurm.schedmd.com/gres.html)
- [Slurm — Scheduling configuration (backfill)](https://slurm.schedmd.com/sched_config.html)
- [Slurm — Preemption](https://slurm.schedmd.com/preempt.html)
- [Slurm — Multifactor priority](https://slurm.schedmd.com/priority_multifactor.html)
- [dstack — Migrate from Slurm](https://dstack.ai/docs/guides/migration/slurm/)
