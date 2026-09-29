# Stage 2B — dstack on the home node

[Labs index](README.md) · Next: [Stage 1 — Kueue on a simulated fleet](stage-1-kueue-simulated-fleet.md) · Context: [Reference scenario](scenario.md)

## Why this lab

The [reference fleet](scenario.md) (32 × p5.48xlarge) is orchestrated with dstack. Running it yourself gives you first-hand answers to three questions: how researchers use it, what it does well, and where it falls short once several teams share a fleet. The lab also prepares the GPU node for every later stage.

By the end you will have:

- the GPU node running the NVIDIA driver, Docker and the NVIDIA Container Toolkit;
- a dstack server on the laptop managing the GPU node as an **SSH fleet**;
- run a dev environment, a checkpointing training task, queued tasks at different priorities, and a utilization policy;
- provisioned and torn down a GPU instance on AWS through the same dstack server.

## Where things run

| Machine | What runs there |
|---|---|
| GPU node | Part A (driver, Docker, NVIDIA toolkit, SSH settings); the checkpoint folder in D2; watching `nvidia-smi` |
| Laptop (WSL2) | Part B onward: the dstack server, every `dstack` command, and all the YAML files, kept in `~/gpu-fleet-lab/dstack` |
| Laptop (PowerShell) | Optional: a second dstack CLI so VS Code links open (D1) |

Every command block below is labelled with its machine. Code blocks contain no prompts, so they can be pasted as they are.

## Concepts

### What dstack is made of

```
laptop (WSL2)                                   GPU node (gpu-node)
┌──────────────────────────────┐   SSH         ┌──────────────────────────────┐
│ dstack CLI  ──►  dstack server│ ────────────► │ dstack shim + runner         │
│ (dstack apply)   state, queue,│               │  └─ Docker container per job │
│                  scheduling   │               │       └─ your code on GPU(s) │
└──────────────────────────────┘               └──────────────────────────────┘
                  │ cloud API (AWS backend)
                  ▼
           EC2 instances it creates and deletes
```

| Piece | Role |
|---|---|
| Server | Holds projects, users, fleets and runs; decides where each run goes. One per organization |
| CLI | `dstack apply -f <file>` sends a YAML configuration to the server; `dstack ps`, `logs`, `stop`, `attach` inspect and control runs |
| Backend | A source of compute the server can create instances in: AWS, other clouds, Kubernetes, Slurm |
| Fleet | A set of instances runs can be placed on. **Backend fleets** are created in a backend and can grow and shrink. **SSH fleets** are machines you already own, reached over SSH |
| Shim and runner | Small agents dstack installs on each instance; they pull the container image and run your commands |
| Run configurations | `dev-environment` (interactive, IDE-attached), `task` (batch job, single or multi-node), `service` (a model endpoint) |

### What dstack decides and what it doesn't

A scheduler answers: *which job runs next, on which machine, and what happens when there isn't room?* dstack's answers:

| Question | dstack's answer |
|---|---|
| Which machine? | Any idle instance in a matching fleet whose resources satisfy the run's `resources` |
| No room? | The run fails with "no capacity", unless `retry` includes `no-capacity`, in which case it waits |
| Which waiting run goes first? | Highest `priority` (0–100), then first-come-first-served |
| Can a high-priority run displace a running one? | No. There is no preemption |
| Can team A be limited to half the fleet? | No. There are no quotas or fair share |
| Can small jobs fill gaps while a big job waits? | No backfill |

Keep that table in mind; exercises 4 and 5 make the last three rows visible.

### Why containers, and what the NVIDIA Container Toolkit does

Every job runs in a container so each researcher can bring their own Python, PyTorch and CUDA libraries without touching the host. A container can't see the GPU by default. The NVIDIA Container Toolkit is a hook that, when Docker starts a container with GPUs requested, mounts the GPU device files (`/dev/nvidia*`) and the host driver's user-space libraries into it. The split to remember:

| Lives on the host | Lives in the container image |
|---|---|
| Kernel driver (`nvidia.ko`) and its user-space library (`libcuda.so`), injected by the toolkit | CUDA runtime, cuDNN, NCCL, PyTorch |

The driver must be at least as new as the CUDA version the image was built for. That single rule explains most "CUDA driver version is insufficient" errors in a fleet.

---

## Part A — Prepare the GPU node

Do this once; stages 2 and the Slurm extension reuse it.

### A1. Ubuntu Server 24.04 LTS

Install to its own drive. During install, enable OpenSSH server. Give the machine a fixed address (DHCP reservation on your router is easiest) so `192.168.1.50` stays valid.

**Why 24.04:** the NVIDIA driver packages, Container Toolkit and GPU Operator all list it as supported. Newer releases can lag in that support.

### A2. NVIDIA driver

Containers use the host's kernel driver, so the GPU node needs the NVIDIA driver before anything else. Ubuntu's `ubuntu-drivers` tool detects the card and installs the driver branch it recommends.

**On the GPU node:**

```bash
sudo apt update && sudo apt install -y ubuntu-drivers-common
ubuntu-drivers devices          # lists the card and the "recommended" driver
sudo ubuntu-drivers install     # installs the recommended one
sudo reboot
```

If Secure Boot is on, the installer asks for a password to enrol a signing key; after reboot a blue MOK screen asks for it. Skipping that step leaves the driver unloaded.

After the reboot, check that the driver loaded and can see the card.

**On the GPU node:**

```bash
nvidia-smi
```

Read the header: **Driver Version** and **CUDA Version**. The CUDA version shown is the newest CUDA the driver supports, not something installed. Note both; they matter when choosing container images. With the second card installed, both GPUs appear with their PCI bus IDs.

Also run `nvidia-smi topo -m` on the GPU node. It prints how the GPUs connect: on your board you'll see something like `PHB` or `NODE` (traffic crosses the PCIe host bridge). On a p5.48xlarge the same command shows `NV18` between every pair (18 NVLink links through NVSwitch). Keep the output; it's the first line of evidence in the NVLink discussion.

The driver version also sets which container images will run. The PyTorch image used throughout these labs is built for CUDA 12.8, which needs driver **570 or later**. If `ubuntu-drivers` chose an older branch, install a newer one explicitly (`ubuntu-drivers list` shows what's available, e.g. `sudo apt install nvidia-driver-580`).

### A3. Docker Engine from Docker's repository

Follow [prerequisites §1.1](00-prerequisites.md#11-docker-engine-from-dockers-repository). If you already installed Ubuntu's `docker.io`, the first block there removes it and Ubuntu's `containerd` before installing Docker's packages.

**Why Docker's repository:** Ubuntu's `docker.io` depends on Ubuntu's `containerd` package; Docker's `docker-ce` depends on Docker's `containerd.io`. They're different builds of the same component with different default configuration, and having parts of both on one machine is what produces containerd start-up errors. Docker's repository also tracks current Docker releases, which is what dstack and the NVIDIA toolkit documentation test against.

**Check, on the GPU node:** `docker version` shows both Client and Server sections, and `docker run --rm hello-world` prints its greeting without `sudo`.

### A4. NVIDIA Container Toolkit

Follow [prerequisites §1.2](00-prerequisites.md#12-nvidia-container-toolkit). Order matters: Docker first, then the toolkit, because the toolkit's last step (`nvidia-ctk runtime configure --runtime=docker`) writes an `nvidia` runtime into Docker's `/etc/docker/daemon.json`.

Check that a container started with `--gpus all` can reach the GPU. This command runs `nvidia-smi` inside a small CUDA base image.

**On the GPU node:**

```bash
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

The same table as on the host, printed from inside a container, proves the toolkit is injecting the driver. Look at `/etc/docker/daemon.json` to see what `nvidia-ctk` changed.

### A5. What dstack needs from an SSH host

| Requirement | Why |
|---|---|
| Docker + NVIDIA Container Toolkit | Jobs run as containers with GPUs attached (done above) |
| Passwordless `sudo` for the SSH user | dstack installs its shim as a system service |
| `AllowTcpForwarding yes` in `/etc/ssh/sshd_config` | The server and CLI reach the runner and your dev environments through SSH tunnels |
| Key-based SSH from the laptop | The server logs in unattended |

Start with the sudo and SSH settings on the node itself.

**On the GPU node:**

```bash
echo "chris ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/chris-nopasswd
sudo grep -n AllowTcpForwarding /etc/ssh/sshd_config   # set to yes if present and "no"
sudo systemctl restart ssh
```

The first line adds a sudoers entry that lets `chris` run any command without a password. If `grep` finds `AllowTcpForwarding no`, change it to `yes` in the file before the restart; if it finds nothing, the default already allows forwarding.

Next, give the laptop key-based access to the node and confirm passwordless sudo works over SSH.

**On the laptop (WSL2):**

```bash
ls ~/.ssh/id_ed25519 || ssh-keygen -t ed25519   # create a key if you have none
ssh-copy-id chris@192.168.1.50
ssh chris@192.168.1.50 'sudo -n true && echo sudo ok'
```

`ssh-copy-id` installs your public key on the node. The last command should print `sudo ok` with no password prompt; `sudo -n` fails instead of prompting, so any error means the sudoers entry is not in effect.

Passwordless sudo is a lab convenience. On a shared fleet you'd scope it to the commands dstack needs.

---

## Part B — dstack server on the laptop

The server needs Linux, so it runs in WSL2. The CLI also runs on native Windows, which matters for VS Code later.

Install the base packages and dstack from [prerequisites §2.2–2.3](00-prerequisites.md#22-base-packages) (git and the OpenSSH client are server requirements; uv installs dstack in its own environment). Then start the server in a WSL2 terminal.

**On the laptop (WSL2):**

```bash
dstack server
```

The server prints its URL (`http://127.0.0.1:3000`) and an **admin token**. Open the URL in a Windows browser; WSL2 forwards localhost. Leave the server running in its own terminal.

In a second WSL2 terminal, point the CLI at the server. Replace `<admin-token>` with the token the server printed.

**On the laptop (WSL2):**

```bash
dstack project add --name main --url http://127.0.0.1:3000 --token <admin-token>
```

**What you've built:** the control plane. Nothing runs anywhere yet, because the server has no compute.

---

## Part C — The GPU node as an SSH fleet

Create the lab repo on the laptop and copy in the shared scripts.

**On the laptop (WSL2):**

```bash
mkdir -p ~/gpu-fleet-lab/dstack && cd ~/gpu-fleet-lab/dstack
cp /path/to/labs/code/train_ddp.py /path/to/labs/code/preflight.py .
```

You are now in `~/gpu-fleet-lab/dstack` with `train_ddp.py` and `preflight.py` beside you. Every YAML file in this lab goes in this directory.

Describe the GPU node as an SSH fleet. The file names the host, the user and key the server logs in with, and how to divide the machine into slots.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/dstack/home-fleet.dstack.yml`

```yaml
type: fleet
name: home
ssh_config:
  user: chris
  identity_file: ~/.ssh/id_ed25519
  hosts:
    - 192.168.1.50
blocks: auto   # one block per GPU: 1 now, 2 once the second 3070 is in
```

The server reads `identity_file` from the laptop, so it must be the same key you copied to the node in A5.

Submit the fleet configuration to the server, then list fleets to see the result.

**On the laptop (WSL2):**

```bash
dstack apply -f home-fleet.dstack.yml
dstack fleet
```

**What happens:** the server SSHes in, installs the shim, inspects the hardware, and registers one instance with its GPUs, CPU, memory and disk. `blocks: auto` splits the instance into one slot per GPU, so two single-GPU jobs can share it.

**Check, on the laptop (WSL2):** `dstack fleet` shows the instance as `idle` with `RTX3070` listed. If it's stuck provisioning, see [Troubleshooting](#troubleshooting).

---

## Part D — Exercises

All `dstack` commands in this part run **on the laptop (WSL2)**, from `~/gpu-fleet-lab/dstack`, where the YAML files are saved. Steps that touch the GPU node say so.

### D1. A dev environment

A dev environment is an interactive container with a GPU that you connect to from an IDE. Define one with a single GPU and the PyTorch image used throughout these labs.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/dstack/dev.dstack.yml`

```yaml
type: dev-environment
name: dev
image: pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
ide: vscode
inactivity_duration: 1h
resources:
  gpu: 1
```

The image is built for CUDA 12.8, so the node needs driver 570 or later (A2).

Submit the configuration to start the environment on the GPU node.

**On the laptop (WSL2):**

```bash
dstack apply -f dev.dstack.yml
```

The output gives a `vscode://` link and an SSH alias. The link only opens VS Code from a CLI on the same OS as VS Code; either use `ssh dev` from WSL2, or install the CLI on Windows too (`uv tool install dstack`, then the same `dstack project add` against `http://127.0.0.1:3000`) and run `dstack apply` from PowerShell to get a working link.

Inside the environment, run `nvidia-smi` and `python -c "import torch; print(torch.cuda.get_device_name(0))"`.

**What to notice:** while this environment is open it holds a GPU whether or not you're using it. That's the most common source of idle GPUs on research fleets. `inactivity_duration` is dstack's answer; note how long 1 hour of idle H100 time costs across 8 GPUs on a p5.

Stop it with `dstack stop dev`.

### D2. A training task with checkpoints

The checkpoint needs to survive the container, so it goes in a directory on the GPU node that the task mounts. Create the directory and give your user ownership of it.

**On the GPU node:**

```bash
sudo mkdir -p /opt/lab-ckpt && sudo chown chris /opt/lab-ckpt
```

Files written to `/opt/lab-ckpt` stay on the node after the container exits.

Define a task that trains on one GPU, mounts `/opt/lab-ckpt` at `/ckpt`, and writes a checkpoint every 200 steps.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/dstack/train.dstack.yml`

```yaml
type: task
name: train
image: pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
files:
  - train_ddp.py
volumes:
  - /opt/lab-ckpt:/ckpt          # instance volume: host path : container path
commands:
  - torchrun --nproc-per-node=$DSTACK_GPUS_PER_NODE train_ddp.py
      --steps 3000 --ckpt-dir /ckpt/train --ckpt-every 200
resources:
  gpu: 1
  shm_size: 8GB
```

`files` copies `train_ddp.py` from the laptop into the container. `$DSTACK_GPUS_PER_NODE` is set by dstack to the number of GPUs the run received, so `torchrun` starts one process per GPU.

Submit the task and follow its log.

**On the laptop (WSL2):**

```bash
dstack apply -f train.dstack.yml
dstack logs train        # in another terminal, to follow output
```

**What to notice:**

- Throughput (`img/s`) and peak memory per GPU in the log. Raise `--batch-size` until memory approaches 8 GB; that's how researchers size jobs to a card.
- `shm_size`: PyTorch data-loader workers pass batches through shared memory. Docker's 64 MB default causes cryptic "bus error" crashes; many first support tickets on a new fleet are this.

**Break it:** `dstack stop train` halfway, then apply again. The log should say `resumed from /ckpt/train/step-N.pt`. The work lost is everything since the last checkpoint: that's the checkpoint-interval trade-off in its simplest form.

### D3. Two jobs share the node (after the second card)

Run two copies of the task at once (`name: train-a`, `name: train-b`, separate `--ckpt-dir`). Each lands on its own block. Then change one to `gpu: 2` and submit it while both are running.

**What to notice:** the 2-GPU run can't start while either block is busy. Without a `retry` policy it fails immediately with no capacity. This is where dstack's queueing begins.

### D4. Priorities without preemption

Occupy the whole node with a long 2-GPU run (or a 1-GPU run before the second card arrives). Then submit three short single-GPU tasks with different priorities, each allowed to wait.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/dstack/low.dstack.yml`

```yaml
type: task
name: low            # repeat as "mid" (priority 50) and "high" (priority 90)
image: pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
files:
  - train_ddp.py
commands:
  - torchrun --nproc-per-node=1 train_ddp.py --steps 300
priority: 10
retry:
  on_events: [no-capacity]
  duration: 2h
resources:
  gpu: 1
```

Save copies as `mid.dstack.yml` and `high.dstack.yml`, changing `name` and `priority` in each. The `retry` block lets a run wait up to two hours for a free GPU instead of failing with no capacity. Submit `low` first, then `mid`, then `high`. Watch `dstack ps`.

**What to notice:**

1. All three wait; the long run keeps going. `high` does not displace it.
2. When the long run ends, `high` starts first even though it arrived last, then `mid`. Priority orders the queue; it never interrupts running work.
3. Nothing prevents one person submitting everything at priority 100.

In a shared research fleet, (1) means an urgent paper-deadline job waits behind a week-long run, and (3) means priority becomes a social agreement rather than a policy. Write down how you'd explain that to a research lead; it's the core of the Kueue argument in the [reference scenario](scenario.md#the-decision-is-two-decisions).

### D5. Utilization policy

dstack can stop runs that hold GPUs without using them. This task is deliberately starved of data, and its `utilization_policy` stops it if GPU utilization stays below 30% for 10 minutes.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/dstack/starved.dstack.yml`

```yaml
type: task
name: starved
image: pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
files:
  - train_ddp.py
commands:
  - torchrun --nproc-per-node=1 train_ddp.py --steps 100000 --data-delay-ms 50 --workers 1
utilization_policy:
  min_gpu_utilization: 30
  time_window: 10m
resources:
  gpu: 1
  shm_size: 8GB
```

`--data-delay-ms 50 --workers 1` makes data loading the bottleneck, so the GPU mostly waits. Watch `nvidia-smi dmon -s u` on the GPU node: utilization in single digits. After the time window dstack terminates the run.

**What to notice:** this policy measures the basic utilization counter, which only says a kernel was running. A job can show 90% by that measure while using a fraction of the tensor cores. On the p5 fleet DCGM's profiling metrics give the truer picture; on GeForce they aren't available ([home lab versus production](scenario.md#home-lab-versus-production)).

Then fix the job: raise `--workers` to 8 and watch utilization climb. That's the conversation you'll have with researchers about "low utilization" tickets.

### D6. The same server, an AWS backend

Needs the AWS account, GPU quota and sign-in from [prerequisites §3](00-prerequisites.md#3-aws-account). Before starting, on the laptop (WSL2), confirm with `aws sts get-caller-identity` (run `aws sso login` first if the session has expired).

Stop the server, then add AWS as a backend in the server's configuration file.

**File on the laptop (WSL2):** `~/.dstack/server/config.yml`

```yaml
projects:
  - name: main
    backends:
      - type: aws
        creds:
          type: default        # uses the AWS credentials in your environment
        regions: [us-east-2]   # the region the labs use
```

Restart `dstack server` in its WSL2 terminal so it loads the backend. The server now uses the credentials you signed in with to create instances in us-east-2.

Create a backend fleet that is empty until needed.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/dstack/aws-lab.dstack.yml`

```yaml
type: fleet
name: aws-lab
nodes: 0..1           # min 0, max 1: provision on demand
backends: [aws]
resources:
  gpu: 1
idle_duration: 10m    # terminate the instance 10 minutes after the last job ends
```

With `nodes: 0..1` the fleet holds no instance, and costs nothing, until a run is placed on it.

To run the training task on AWS, copy `train.dstack.yml` to `train-aws.dstack.yml` and edit the copy: change `name` to `train-aws`, add the line `fleets: [aws-lab]`, and delete the `volumes` entry, since that host path exists only on the GPU node. Then register the fleet and submit the task.

**On the laptop (WSL2):**

```bash
dstack apply -f aws-lab.dstack.yml
dstack apply -f train-aws.dstack.yml
```

The first command creates the empty fleet. The second shows a plan listing matching offers with their prices before it asks for confirmation; the cheapest single-GPU G instance is usually chosen. dstack then launches the instance, pulls the image and starts the task.

**What to notice:**

- The plan table: dstack shops across instance types and prices. On the reference fleet the p5 capacity comes from Capacity Blocks bought through a central procurement team; understand how dstack is pointed at reserved capacity (look for reservation settings in the AWS backend and fleet reference) rather than shopping on-demand.
- Time from `apply` to first log line: instance boot, image pull (the PyTorch image is several GB), then the job. Image pull time on a fresh node is a real cost on a 32-node fleet; teams pre-bake images or use a registry mirror.
- In the EC2 console, the instance, its security group and tags dstack created. Ten minutes after the run ends, the instance is gone.

**Clean up:** `dstack fleet delete aws-lab` and confirm in the EC2 console that nothing remains.

### D7. dstack on Kubernetes (after stage 2)

Once the k3s cluster from [stage 2](stage-2-k8s-gpu-node.md) exists, this exercise tests whether dstack can sit on top of Kueue. It's written up there as [exercise E8](stage-2-k8s-gpu-node.md#e8-dstack-on-top-of-kueue).

---

## Record

Answer in `gpu-fleet-lab/notes/dstack.md`:

1. How long from `dstack apply` to the first training log line, at home and on AWS? Where did the time go?
2. What happened to `high` in D4, and what would a researcher with a deadline experience?
3. What does D5's policy catch, and what does it miss?
4. List what you'd need to add for three research groups sharing 32 nodes: quotas, fair share, preemption, cost attribution, something else?
5. What did you like about dstack as a researcher? Be specific; the people who chose it will want to hear it.

## Clean up

Stop any runs still active and remove the home fleet from the server.

**On the laptop (WSL2):**

```bash
dstack stop --all            # if any runs remain
dstack fleet delete home     # removes the shim registration; the machine is untouched
```

Leave Docker and the NVIDIA toolkit installed; stage 2 builds on them.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| SSH fleet stuck provisioning | Key not accepted, sudo prompts for a password, or TCP forwarding off | Run the checks in A5 by hand from WSL2 |
| `containerd` fails to start, or `docker.service` fails after install | Ubuntu's `docker.io`/`containerd` mixed with Docker's packages, or leftover containerd config | [Prerequisites §1.1](00-prerequisites.md#11-docker-engine-from-dockers-repository): purge the Ubuntu packages, reinstall from Docker's repo, check `journalctl -u containerd` |
| `could not select device driver "" with capabilities: [[gpu]]` | Container toolkit not registered with Docker, or Docker reinstalled after the toolkit | Rerun `sudo nvidia-ctk runtime configure --runtime=docker` and restart Docker |
| `CUDA driver version is insufficient` | Image built for newer CUDA than the driver supports | Compare the image tag with the CUDA version in `nvidia-smi`; pick an older image or update the driver |
| `Bus error` in the data loader | Shared memory too small | Set `shm_size` in `resources` |
| Run fails immediately with no capacity | No free block and no retry policy | Add `retry: on_events: [no-capacity]` |
| `vscode://` link does nothing | CLI in WSL2, VS Code on Windows | Use `ssh dev`, or run the CLI on Windows |
| AWS run finds no offers | GPU quota still zero, or region lacks the type | Check Service Quotas in us-east-2 |

## References

- [dstack — Installation](https://dstack.ai/docs/installation/)
- [dstack — Fleets](https://dstack.ai/docs/concepts/fleets/)
- [dstack — Tasks](https://dstack.ai/docs/concepts/tasks/)
- [dstack — Dev environments](https://dstack.ai/docs/concepts/dev-environments/)
- [dstack — Backends](https://dstack.ai/docs/concepts/backends/)
- [dstack — Migrate from Slurm](https://dstack.ai/docs/guides/migration/slurm/)
- [NVIDIA Container Toolkit — install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
