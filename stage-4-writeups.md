# Stage 4 — Write-ups

[Labs index](README.md) · Previous: [Stage 3 — EKS GPU cluster](stage-3-eks-gpu-cluster.md) · Context: [Reference scenario](scenario.md)

## Why this lab

Runbooks, decision records and onboarding documents are how a fleet team makes its decisions and operations repeatable. Writing them for the [reference scenario](scenario.md), from evidence you gathered in stages 1–3, turns the labs into something reviewable. Each document below has a template and guidance on what good looks like; fill them from your notes in `gpu-fleet-lab/notes/`.

| Document | Location | Built from |
|---|---|---|
| Scheduler decision record | `docs/adr-001-scheduler.md` | Stages 2B, 1, 2 (E8), Slurm extension |
| GPU node failure runbook | `docs/runbook-gpu-node-failure.md` | Stage 2 E3–E4, E7; stage 3 E2 |
| Preflight check | `scripts/preflight.py` + `docs/preflight.md` | [code/preflight.py](code/preflight.py); stage 2 E2; stage 3 E1 |
| Researcher onboarding | `docs/onboarding.md` | Stage 2B D1–D2 |

---

## 1. Decision record: scheduling for the research fleet

### What a decision record is for

A decision record captures one significant decision: the situation that forced it, the options considered, what was chosen, and what follows from the choice. Its reader is someone a year from now asking "why did we do it this way?" It should be short enough to read in five minutes and specific enough that the reasoning can be checked.

### Template

```markdown
# ADR-001: Workload scheduling and governance for the research GPU fleet

Status: Proposed | Date: YYYY-MM-DD | Author: Christian Lesemann, CD

## Context
- Fleet: ~256 H100 as 32 x p5.48xlarge in us-east-2, plus single-GPU p5.4xlarge; capacity bought as
  Capacity Blocks through central procurement.
- Today: dstack orchestrates all work. Researchers use dev environments and tasks.
- Users: <number> research groups; <some> ask for Slurm.
- Problem: <what measurement or complaint shows the need — contention, idle GPUs, waiting deadline work>.
- Constraints: organization security controls; team runs its own estate; <others>.

## Decision drivers
1. Fair, enforceable sharing between groups (quotas, borrowing, reclaim).
2. Deadline work can displace batch work (preemption).
3. Researcher experience does not get worse.
4. Operable by a small team.
5. Works with Capacity Block lifecycles and multi-node EFA placement.

## Options considered
| Option | Front end | Governance | Pros | Cons | Evidence |
|---|---|---|---|---|---|
| A. dstack as is | dstack | priorities only | ... | ... | Lab 2B D4 |
| B. dstack over EKS + Kueue | dstack | Kueue | ... | ... | Lab 2 E8 |
| C. EKS + Kueue, SkyPilot/Lightning front end | ... | Kueue | ... | ... | Lab 1, 3 |
| D. Slurm (Slinky / HyperPod / ParallelCluster) | sbatch | Slurm | ... | ... | Slurm extension |
| E. Split fleet | mixed | mixed | ... | ... | — |

## Decision
<Chosen option, or "measure first, then pilot B on a 4-node slice" with the criteria that decide the next step.>

## Consequences
- What gets better:
- What gets worse or harder:
- What we must build or operate:
- What we will measure to know it worked:

## Open questions
- ...
```

### Guidance

- **Put evidence in the Evidence column.** "Lab 2B D4: a priority-90 run waited 40 minutes behind a running job" is stronger than "dstack lacks preemption".
- **The decision can be a staged one.** "Measure contention for 30 days; if pending time for any group exceeds X hours/week, pilot option B" is a legitimate decision with a clear trigger.
- **Write consequences honestly.** Every option adds something to operate. Kueue means running EKS; Slurm means running `slurmctld`, `slurmdbd` and a database; SkyPilot or Lightning means a migration for every researcher.
- **Keep it to two pages.** Put detail in appendices or link to lab notes.

---

## 2. Runbook: GPU node failure

### What a runbook is for

A runbook is followed at 3 a.m. by someone who may not have seen this failure before. It needs to be ordered, with commands to run and decisions to make at each step, and it needs to say when to stop and escalate.

### Template

```markdown
# Runbook: GPU node failure or suspected bad GPU

Applies to: research fleet (p5.48xlarge, p5.4xlarge), us-east-2
Owner: Research infrastructure | Last tested: YYYY-MM-DD

## Triggers
- Node monitoring agent: AcceleratedHardwareReady = False
- Xid in kernel log or DCGM_FI_DEV_XID_ERRORS non-zero
- NCCL timeout or hang reported by a job; preflight failure
- Researcher report: "job slow / hung on node X"

## 1. Contain (5 minutes)
- Stop new work landing on the node:  kubectl cordon <node>   (dstack: <equivalent>)
- Identify affected jobs:  kubectl get pods -A -o wide --field-selector spec.nodeName=<node>
- Notify job owners in #research-infra with node, jobs, expected impact.

## 2. Capture evidence (10 minutes) — before any reboot
- Instance ID, instance type, AZ, Capacity Block / reservation ID
- Xid lines:  dmesg -T | grep -i xid   (or node monitoring agent events)
- nvidia-smi -q -d ECC,ROW_REMAPPER,PERFORMANCE
- dcgmi diag -r 2   (on p5; takes minutes)
- NCCL logs from the failed job
Save to: <evidence location>, named <date>-<instance-id>.

## 3. Classify
| Signal | Class | Action |
|---|---|---|
| Xid 13, 31, 43 | Usually application | Return to job owner with evidence; uncordon after a clean preflight |
| Xid 48, 94, 95 | Memory ECC | Drain; reset GPU / reboot; burn-in; replace if it recurs |
| Xid 63, 64 | Row remapping | 63: reset. 64: replace |
| Xid 74 | NVLink | Drain; diag; usually replace |
| Xid 79 | GPU off the bus | Replace |
| Hang, no Xid | Unknown | nccl-tests across node subsets to find the slow link |

## 4. Remediate
- Reboot path: drain, reboot, burn-in (preflight + dcgmi diag -r 3), uncordon.
- Replace path: let node auto repair act, or terminate and request remediation within the Capacity Block
  through AWS Support / the capacity-block channel with the evidence packet.

## 5. Recover affected jobs
- Requeue from last checkpoint; confirm with owner.
- Record GPU-hours lost = nodes x GPUs x (time since last checkpoint + time to restart).

## 6. Close
- Ticket updated with evidence, class, action, time to restore.
- If the failure could have been caught earlier (preflight, alert), file the improvement.

## Escalate when
- More than one node in the same block fails within 24 h.
- Replacement not provided within <agreed time>.
- Any data-loss risk to checkpoints.
```

### Guidance

- **Test it.** Walk the stage 2 E4 hang and E7 cordon through the runbook and fix every step that didn't match reality. Record "Last tested" with the date.
- **Evidence before reboot.** A reboot clears GPU error state; the evidence packet is what gets AWS to replace hardware quickly.
- **Measure time to restore.** It's the runbook's own SLI, and the number to improve.

---

## 3. Preflight check

### Design

[code/preflight.py](code/preflight.py) already exists. What's left is explaining it and wiring it in. Read the code first; each check is commented with the reason for it.

| Check | Catches | Misses |
|---|---|---|
| `nvidia-smi` health (ECC, row remap, temperature, throttling) | Known-bad GPUs, overheating nodes | Faults that only appear under load |
| Matrix multiply vs CPU | Grossly faulty compute | Subtle numerical errors |
| NCCL all-reduce, verified and timed | Broken or slow links, rendezvous problems, mis-set networking | Intermittent faults after the check passes |

### Wiring it in

| Scheduler | How to run preflight before the job |
|---|---|
| Kubernetes | Run it as the first command in the job (`torchrun ... preflight.py && torchrun ... train.py`). An init container runs per pod and can't do the cross-pod all-reduce |
| dstack | Prefix the task's `commands` with the preflight `torchrun` line |
| Slurm | `srun` it at the top of the `sbatch` script; clusters also use a `Prolog` script for per-node checks |

For multi-node jobs the all-reduce must use the same shape as the job (same nodes, same ranks), which is why it runs as part of the job rather than separately.

### What to write in `docs/preflight.md`

1. What it checks and why (the table above, in your words).
2. How long it takes at 1, 2 and 8 GPUs (measure it; stage 2 and 3 give you two data points).
3. Thresholds: what `--min-busbw-gbps` would you set for p5.48xlarge? Look up published nccl-tests results for H100 with NVSwitch and EFA and pick a conservative floor.
4. What happens on failure: which node is reported, and how a researcher gets it looked at.

---

## 4. Researcher onboarding

### Who it's for

A researcher who knows PyTorch and has never used this fleet. The goal is their first real job running within an hour, and knowing where to ask for help.

### Outline

```markdown
# Getting started on the research GPU fleet

## What you get
- The fleet in one paragraph: GPU types, how many, how they're shared.
- Your team's quota and what happens when you exceed it.

## Set up (15 minutes)
- Access request, CLI install, first login.

## Your first run (15 minutes)
- Dev environment for interactive work: config, launch, attach from VS Code, stop.
- A training task: config, launch, logs, stop.

## Running real jobs
- Choosing GPUs and nodes; single vs multi-node.
- Checkpoints: where, how often, and why (preemption, node failure, block end times).
- Data: where datasets live, how to read them fast.
- Priorities: batch vs deadline, and who approves deadline.

## When things go wrong
- The five most common errors and their fixes (shm, CUDA version, no capacity, OOM, NCCL timeout).
- How to report a bad node: what to include.
- Office hours and support channel.

## Etiquette
- Stop idle dev environments; the utilization policy will.
- Honest time limits and job sizes.
```

### Guidance

- **Write it from what confused you.** Every snag in stage 2B (VS Code link, `shm_size`, image choice) is a line in "when things go wrong".
- **Every command must be copy-pasteable and tested.**
- **Link, don't repeat.** Point to dstack's own docs for syntax; this document covers your fleet's conventions.

---

## Using the documents in a design review

| Question you'll be asked | Document to draw on |
|---|---|
| "How would you build the scheduling layer?" | ADR-001, especially the evidence column |
| "Walk me through a GPU failure." | The runbook, step by step, with evidence-before-reboot |
| "How do you reduce support load?" | Preflight and onboarding, with the common-errors list |
| "What would your first 90 days look like?" | ADR's "measure first" and the runbook's time-to-restore |

Be clear about the source: lab evidence from two consumer GPUs and a small EKS cluster, with the differences from production stated.

## References

- [Michael Nygard — Documenting architecture decisions](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
- [Google SRE Workbook — On-call and runbooks](https://sre.google/workbook/on-call/)
- [NVIDIA — Xid errors](https://docs.nvidia.com/deploy/xid-errors/index.html)
- [nccl-tests](https://github.com/NVIDIA/nccl-tests)
- [EKS — Node auto repair](https://docs.aws.amazon.com/eks/latest/userguide/node-repair.html)
