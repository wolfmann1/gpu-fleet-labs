# GPU fleet labs

[Reference scenario](scenario.md) · [Prerequisites](00-prerequisites.md)

Hands-on labs for operating a shared GPU research fleet: scheduling, the NVIDIA software stack, failure handling, and the AWS layer. Each guide explains what a component does and why it exists before asking you to install it, and each exercise states what you should observe and what it means for a production fleet like the one in the [reference scenario](scenario.md).

## Why this repo exists

I'm Christian Lesemann. I spent 25 years running source-control and build infrastructure for game studios, most recently as a senior manager and lead architect at EA, ZeniMax and Xbox. GPU research fleets share most of that job's problems (scheduling scarce compute fairly, finding and removing the causes of failure, supporting the people who depend on the platform) and add some of their own: gang scheduling, NCCL, GPU hardware faults, and capacity that's bought weeks in advance.

These labs are how I'm closing that gap, in the open. They're written so an engineer joining a GPU platform team can work through them too: every step says which machine it runs on, why it's there, and what to look for. The [status table](#status) shows how far I've got; issues and pull requests with corrections are welcome.

## Labs in working order

| Order | Lab | Machine | Time | Main idea |
|---|---|---|---|---|
| — | [Reference scenario](scenario.md) | — | 20 minutes | The fleet the labs are measured against, and the scheduling decision it faces |
| 0 | [Prerequisites](00-prerequisites.md) | Both | 1–2 hours | Every package repository and tool the labs use, installed one way |
| 1 | [Stage 2B — dstack on the home node](stage-2b-dstack.md) | GPU node + laptop | 2–3 evenings | A researcher-facing orchestrator, and where it stops |
| 2 | [Stage 1 — Kueue on a simulated fleet](stage-1-kueue-simulated-fleet.md) | Laptop | 3–4 evenings | Quotas, borrowing, preemption and gang admission at 32-node scale |
| 3 | [Stage 2 — Kubernetes GPU node](stage-2-k8s-gpu-node.md) | GPU node | 1 weekend + drills | The NVIDIA software stack, real multi-GPU training, failure drills |
| 4 | [Extension — an evening of Slurm](extension-slurm.md) | GPU node | 1 evening | What the researchers asking for Slurm would miss |
| 5 | [Stage 3 — EKS GPU cluster](stage-3-eks-gpu-cluster.md) | AWS us-east-2 | 2 sessions | The AWS-specific layer: node groups, health agent, multi-node, cost |
| 6 | [Stage 4 — Write-ups](stage-4-writeups.md) | Laptop | 2 evenings | Decision record, runbook, preflight, onboarding |

Stage 2B comes first because the reference fleet already runs dstack, and it needs only Docker on the GPU node. Stage 1 can run on the laptop in parallel.

## Using these labs yourself

| You want | Do this |
|---|---|
| Your own copy to work through, with your own notes and status | **Use this template** (green button on GitHub) → create a repository under your account, public or private. It starts with a single commit and no link back here |
| To suggest a correction or improvement | **Fork**, change, and open a pull request |
| Just to read and follow along | Clone it, or read it on GitHub |

If you copy it, reset the status table below to your own progress.

## Status

| Lab | Status |
|---|---|
| Prerequisites | In progress: GPU node and laptop tooling installed, AWS account and Identity Center sign-in done, GPU quota requested |
| Stage 2B — dstack | In progress |
| Stage 1 — Kueue simulation | Not started |
| Stage 2 — Kubernetes GPU node | Not started |
| Extension — Slurm | Not started |
| Stage 3 — EKS | Waiting on GPU quota |
| Stage 4 — Write-ups | Not started |

The guides were written in September 2026 against current documentation and are corrected as each one is run. The two scripts in `code/` have been checked for syntax but not yet run on GPUs.

## Hardware and adapting the labs

| Used here | Substitute with |
|---|---|
| Ubuntu Server 24.04 box with two RTX 3070 (8 GB) | Any Linux machine with one or more NVIDIA GPUs of the Turing generation or newer and 8 GB or more; one GPU covers everything except the multi-GPU exercises |
| Windows laptop running WSL2 Ubuntu 24.04 | Any Linux or macOS workstation; skip the WSL-specific notes |
| Personal AWS account in us-east-2 | Any region offering g5/g6 instances; change the region in the Terraform and dstack configs |

## Lab-only settings

A few choices trade security or resilience for convenience and are called out where they occur: passwordless `sudo` on the GPU node, the AdministratorAccess permission set, a single NAT gateway, and public EKS API access. Don't carry them into a shared environment.

## Shared code

| File | Used in | Purpose |
|---|---|---|
| [code/train_ddp.py](code/train_ddp.py) | Stages 2B, 2, 3 | Small distributed training job with checkpoint and resume, throughput logging, and a switch to simulate slow data loading |
| [code/preflight.py](code/preflight.py) | Stages 2, 3, 4 | GPU health and NCCL all-reduce check to run before a job starts |

Both run anywhere PyTorch with CUDA is available, from one GPU up to several nodes, under `torchrun`.

## How each guide is laid out

| Section | Contents |
|---|---|
| Why this lab | What the stage proves and how it maps to running a production fleet |
| Concepts | The ideas you need before touching anything |
| Build | Steps, each with an explanation of what it does and a check that it worked |
| Exercises | Things to try, what to watch for, and what the result means |
| Record | Questions to answer in your own notes; these feed the [stage 4 write-ups](stage-4-writeups.md) |
| Clean up and troubleshooting | Teardown and the problems you're most likely to meet |

## Conventions

| Placeholder | Meaning |
|---|---|
| `gpu-node` / `192.168.1.50` | The GPU node (Ubuntu 24.04, RTX 3070s). Substitute its real hostname and LAN address |
| `chris` | Your Linux user on the GPU node |
| **On the GPU node:** | The command block below runs on the Ubuntu box with the RTX 3070s (console or `ssh chris@192.168.1.50`) |
| **On the laptop (WSL2):** | The command block below runs in the Ubuntu terminal inside WSL2 on your laptop |
| **File on …** | The block below is the contents of a file to create on that machine |
| Inside the pod | Commands run in a shell opened with `kubectl exec` |

Code blocks contain no shell prompts, so they can be pasted as they are. Each lab also opens with a short "Where things run" section.

## Working with YAML

Most files in these labs are YAML, and YAML uses indentation to show structure. Three rules prevent nearly every error:

| Rule | Why |
|---|---|
| Indent with spaces, never tabs | The YAML specification forbids tabs for indentation; a tab produces an error such as `found character that cannot start any token` |
| Keep the indentation exactly as shown, two spaces per level | A key indented one space too far or too little becomes a child of the wrong parent, or a syntax error |
| Paste whole blocks into a file rather than retyping them | Copying a block keeps its indentation intact |

Editors can change indentation as you paste. In nano, it's safe by default. In vi or vim, run `:set paste` before pasting so auto-indent doesn't add spaces to every line. `kubectl edit` opens vi unless told otherwise; to use nano for the whole session, run `export KUBE_EDITOR=nano` (add it to `~/.bashrc` to keep it).

To check a YAML file before using it, on the laptop (WSL2): `python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))" file.yaml`. No output means the file parses; an error names the line and column to fix. For Kubernetes manifests, `kubectl apply --dry-run=server -f file.yaml` goes further and checks the objects against the cluster.

Version numbers were current on 2026-09-28. Where a newer release exists, use it and read its release notes for renamed fields; Kueue in particular moved its API to `v1beta2` and renamed `cohort` to `cohortName`.

Keep your notes, manifests and results in a `gpu-fleet-lab` working folder so the stage 4 documents can cite them.

## Licence

Scripts in `code/` are under the [MIT licence](LICENSE). The guides are under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): reuse and adapt them with attribution.
