# Reference scenario

[Labs index](README.md) · [Prerequisites](00-prerequisites.md)

The labs are built around one realistic situation, so each exercise has something concrete to be measured against. The scenario is illustrative; the numbers are typical of a mid-sized research group on AWS.

## The fleet

| Aspect | Reference fleet |
|---|---|
| GPUs | ~256 NVIDIA H100 as 32 × p5.48xlarge (8 GPUs each, NVSwitch inside the node, 3,200 Gbps EFA between nodes), plus a few single-GPU p5.4xlarge for development |
| Region | One AWS region (us-east-2 in the labs); multi-node jobs stay in one Availability Zone |
| Capacity | EC2 Capacity Blocks for ML and reservations, bought through a central procurement team; blocks are booked weeks ahead and end at a fixed time |
| Users | Several research groups sharing the fleet |
| Orchestration today | dstack |
| Under evaluation | Kubernetes (EKS) with Kueue; Slurm, requested by researchers with an HPC background; SkyPilot and Lightning AI as researcher front ends |
| Operating model | The fleet team runs its own AWS estate within the organization's security controls |

## dstack in one page

dstack is an open-source orchestrator for AI workloads. It provisions GPU instances from cloud accounts, or uses machines you already have, and runs containers on them.

| Concept | What it does |
|---|---|
| Server and projects | A dstack server holds state, users and projects; the CLI (`dstack apply`) submits YAML configurations to it |
| Backends | Where compute comes from: AWS and other clouds as VMs; Kubernetes, Slurm, Runpod and Vast.ai as container backends |
| Fleets | Groups of instances. Backend fleets are provisioned from a cloud; SSH fleets are existing machines reached over SSH |
| `placement: cluster` | Co-locates instances for multi-node work; on AWS, dstack enables EFA on instance types that support it |
| `blocks` | Splits an instance so several jobs share it, e.g. four 2-GPU slots on an 8-GPU node |
| Run types | Dev environments (interactive), tasks (batch, single or multi-node), services (endpoints) |
| Scheduling | Priority 0–100, first-in-first-out within a level; `retry` on `no-capacity` queues a run until capacity appears |

dstack's own Slurm migration guide lists what it doesn't do: per-user or per-team quotas, fair share, backfill, preemption and graceful stop signals. For one team those gaps are tolerable; for several groups competing for one fleet they're where the friction shows.

## The decision is two decisions

The tools under evaluation sit at two different layers:

| Layer | Question it answers | Candidates |
|---|---|---|
| Researcher front end | How does a researcher describe and launch a job? | dstack, SkyPilot, Lightning AI, `sbatch`, raw Kubernetes manifests |
| Resource governance | Who gets which GPUs, when, and what happens under contention? | dstack priorities only, Kueue on EKS, Slurm |

Some combinations stack: dstack lists Kubernetes and Slurm as backends, and SkyPilot runs on both. Whether jobs submitted through dstack's Kubernetes backend can be governed by Kueue is tested in [stage 2, exercise E8](stage-2-k8s-gpu-node.md#e8-dstack-on-top-of-kueue).

| Option | Front end | Governance | For | Against |
|---|---|---|---|---|
| A. dstack as is | dstack | Priorities, plus team agreements | No migration | No quotas, fair share or preemption |
| B. dstack over EKS + Kueue | dstack | Kueue | Keeps the researcher experience; adds quotas, borrowing, preemption, topology-aware placement | EKS to run; integration unproven |
| C. EKS + Kueue with SkyPilot or Lightning | SkyPilot / Lightning | Kueue | Mature Kubernetes ecosystem; SkyPilot also reaches other clouds | Migration for every researcher |
| D. Slurm (Slinky on EKS, SageMaker HyperPod, or ParallelCluster) | `sbatch` | Slurm fair share, QOS, preemption, backfill | Strongest scheduling policy; familiar to HPC researchers | Separate from the rest of the platform tooling |
| E. Split fleet | dstack for most, Slurm partition for some | Mixed | Each group keeps its habits | Two systems to run; capacity stranded between them |

A defensible way to decide: measure contention on the current fleet first; if groups do contend, pilot option B on a small slice (a few of the 32 nodes); size the Slurm demand before building for it; compare front ends on researcher time to a first multi-node run; record the outcome as a decision record ([stage 4](stage-4-writeups.md)).

## Home lab versus production

The labs run on two consumer RTX 3070s and a small EKS cluster. What that setup shows, and what it can't:

| Works in the lab | Differs from a p5 fleet |
|---|---|
| Kubernetes GPU scheduling (`nvidia.com/gpu`) | No NVLink on GeForce; GPU-to-GPU traffic goes through host memory because GeForce drivers don't expose PCIe peer-to-peer |
| Multi-GPU and two-node PyTorch training with NCCL | No EFA; two-node traffic in stage 3 runs over TCP |
| Time-slicing one GPU into several | No MIG partitioning (data-centre cards only) |
| DCGM exporter basic fields: utilization, memory, temperature, power, clocks, Xid | No DCGM profiling metrics (`DCGM_FI_PROF_*`, e.g. SM and tensor-core activity); data-centre GPUs only |
| Xid errors in the kernel log | `dcgmi diag` hardware diagnostics may not run on GeForce |

State these differences whenever you present lab results.

## Sources

- [dstack — Fleets](https://dstack.ai/docs/concepts/fleets/)
- [dstack — Backends](https://dstack.ai/docs/concepts/backends/)
- [dstack — Migrate from Slurm](https://dstack.ai/docs/guides/migration/slurm/)
- [AWS — Capacity Blocks for ML](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html)
- [NVIDIA dcgm-exporter issue #506 — profiling metrics on GeForce](https://github.com/NVIDIA/dcgm-exporter/issues/506)
