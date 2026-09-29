# Stage 2 — A Kubernetes GPU node at home

[Labs index](README.md) · Previous: [Stage 1 — Kueue on a simulated fleet](stage-1-kueue-simulated-fleet.md) · Next: [Extension — Slurm](extension-slurm.md)

## Why this lab

Stage 1 showed Kueue's decisions against imaginary GPUs. This lab puts real GPUs underneath: the NVIDIA software stack on Kubernetes, a real two-GPU training job, GPU metrics, and a set of failure drills. It's also where you test whether dstack can sit on top of Kueue, the open question in the [reference scenario](scenario.md#the-decision-is-two-decisions).

By the end you will have:

- k3s on the GPU node with both RTX 3070s schedulable as `nvidia.com/gpu`;
- the GPU Operator, DCGM exporter, Prometheus and Grafana running;
- Kueue governing two teams' access to the two GPUs;
- trained across both GPUs with NCCL, then crashed, hung, throttled and oversubscribed that training on purpose;
- tried dstack's Kubernetes backend against Kueue.

Prerequisite: [stage 2B Part A](stage-2b-dstack.md#part-a--prepare-the-gpu-node) (driver 570+, Docker from Docker's repository, NVIDIA Container Toolkit; install details in [prerequisites §1](00-prerequisites.md#1-gpu-node-ubuntu-server-2404)). k3s needs the toolkit, not Docker; Docker stays for dstack. The lab works with one card; E2 and E5 need two.

## Where things run

| Machine | What runs there |
|---|---|
| GPU node | k3s itself (B1), `nvidia-smi` checks, and uninstalling k3s at the end |
| Laptop (WSL2) | Everything else: `kubectl`, `helm`, Grafana port-forwarding, and the YAML files, kept in `~/gpu-fleet-lab/k8s`. kubectl reaches the GPU node's cluster through the kubeconfig copied in B1 |
| Inside a pod | E4 only, after `kubectl exec` from the laptop |

Every command block below is labelled with its machine.

## Concepts

### The GPU stack on a Kubernetes node

```
 Pod requesting nvidia.com/gpu: 2
        │  scheduled because the node advertises nvidia.com/gpu: 2
        ▼
 kubelet ──► containerd ──► nvidia-container-runtime ──► container sees /dev/nvidia0, /dev/nvidia1
                                   ▲                                     + host libcuda.so
                                   │ (NVIDIA Container Toolkit)
 Device plugin (DaemonSet) ── tells kubelet "this node has 2 GPUs" and which device IDs to hand each pod
 GPU Feature Discovery ────── labels the node: gpu.product, gpu.memory, cuda driver version…
 DCGM exporter ───────────── publishes per-GPU metrics for Prometheus
 Host: NVIDIA kernel driver
```

| Component | Job | Who installs it here |
|---|---|---|
| Kernel driver | Talks to the hardware | You, on the host (stage 2B) |
| Container Toolkit / runtime | Injects GPUs and driver libraries into containers | You, on the host (stage 2B) |
| Device plugin | Advertises `nvidia.com/gpu` to Kubernetes and assigns specific GPUs to pods | GPU Operator |
| GPU Feature Discovery (GFD) | Labels nodes with GPU model, memory, driver | GPU Operator |
| DCGM exporter | GPU metrics on an HTTP endpoint for Prometheus | GPU Operator |
| GPU Operator | Installs and keeps the above consistent across nodes | Helm |

The GPU Operator can also install the driver and toolkit itself, as containers. You'll disable that, because the host already has them. That's the same split AWS uses: the EKS-optimized NVIDIA AMI ships with driver and toolkit, and you add the device plugin. What you build here maps directly onto [stage 3](stage-3-eks-gpu-cluster.md).

### Why GPUs are whole numbers

`nvidia.com/gpu` is an **extended resource**: an integer count the scheduler adds and subtracts. A pod can't ask for half a GPU, and Kubernetes has no idea how much memory or compute a pod uses once it has one. Sharing a GPU therefore needs a trick (time-slicing, E5) or hardware partitioning (MIG, data-centre cards only).

### What NCCL does with two GPUs

In data-parallel training every GPU holds a copy of the model. After each backward pass, NCCL all-reduces the gradients so every copy applies the same update. How fast that goes depends on the path between GPUs:

| Path | Where you'd see it | Relative speed |
|---|---|---|
| NVLink through NVSwitch | p5.48xlarge (all 8 GPUs) | Fastest, by an order of magnitude |
| PCIe peer-to-peer | Data-centre cards on the same PCIe switch | Middle |
| Through host memory (`SHM`) | GeForce cards, which don't expose peer-to-peer | Slowest |

With two 3070s you'll see the last one. E2 makes NCCL tell you which path it chose.

---

## Build

### B1. k3s

k3s is a complete Kubernetes distribution in a single binary, with containerd built in. On start-up it looks for the NVIDIA container runtime on the host and, if found, registers it with containerd and creates a `RuntimeClass` named `nvidia`. Making it the default runtime means every pod gets GPU access plumbing without each manifest naming the runtime class.

**On the GPU node:**

```bash
sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/config.yaml <<'EOF'
default-runtime: nvidia
write-kubeconfig-mode: "0644"
EOF
curl -sfL https://get.k3s.io | sh -
kubectl get nodes
kubectl get runtimeclass
sudo grep -n nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml
```

**Check, from the output above (GPU node):** the node is `Ready`, `nvidia` appears among the runtime classes, and the containerd config references `nvidia-container-runtime`.

k3s uses its own containerd, separate from the Docker you installed in stage 2B. Both can coexist; dstack's SSH fleet uses Docker, Kubernetes uses k3s's containerd.

Copy the kubeconfig k3s wrote to the laptop and replace its loopback address with the node's LAN address. From then on, kubectl and Helm on the laptop drive the cluster.

**On the laptop (WSL2):**

```bash
scp chris@192.168.1.50:/etc/rancher/k3s/k3s.yaml ~/.kube/home.yaml
sed -i 's/127.0.0.1/192.168.1.50/' ~/.kube/home.yaml
export KUBECONFIG=~/.kube/home.yaml
kubectl get nodes
```

kubectl and Helm come from [prerequisites §2.4–2.5](00-prerequisites.md#24-kubectl) if stage 1 didn't already install them. kubectl v1.36 matches the k3s release the install script currently fetches; `kubectl version` shows both client and server versions, which should be within one minor version of each other.

### B2. Prometheus and Grafana first

Install kube-prometheus-stack before the GPU Operator. The operator will create a ServiceMonitor for the DCGM exporter, and that object type only exists once kube-prometheus-stack is installed.

**On the laptop (WSL2):**

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm repo update
helm install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
```

Helm installs Prometheus, Grafana and the Prometheus Operator into the `monitoring` namespace. The `--set` lets Prometheus pick up ServiceMonitors from other Helm releases, including the GPU Operator's; stage 1 explains it in full.

### B3. GPU Operator

Install the GPU Operator with Helm, using the driver and toolkit already on the host. `--wait` holds the command until the operator's pods are ready.

**On the laptop (WSL2):**

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia && helm repo update
helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace \
    --version=v26.7.1 \
    --set driver.enabled=false \
    --set toolkit.enabled=false \
    --set dcgmExporter.serviceMonitor.enabled=true \
    --wait
kubectl -n gpu-operator get pods
```

The final command lists the pods the operator created: device plugin, GFD, DCGM exporter and validators. The table gives the reason for each flag.

| Flag | Reason |
|---|---|
| `driver.enabled=false` | Host driver already installed |
| `toolkit.enabled=false` | Host toolkit already installed and k3s already uses it |
| `dcgmExporter.serviceMonitor.enabled=true` | Prometheus scrapes GPU metrics automatically |

Check the result in order: the operator's pods, then the GPU count the node advertises, then the labels GFD added to the node.

**On the laptop (WSL2):**

```bash
kubectl -n gpu-operator get pods                      # validator pods Completed, the rest Running
kubectl describe node gpu-node | grep -A8 Allocatable  # nvidia.com/gpu: 2
kubectl get node gpu-node --show-labels | tr ',' '\n' | grep nvidia.com
```

The labels from GFD include `nvidia.com/gpu.product=NVIDIA-GeForce-RTX-3070`, `nvidia.com/gpu.memory`, `nvidia.com/gpu.count` and the driver version. On a mixed fleet those labels are how you target GPU types, and Kueue flavors use them.

Run a first GPU pod that requests one GPU and runs `nvidia-smi`. `--rm` deletes the pod when the command exits.

**On the laptop (WSL2):**

```bash
kubectl run smi --rm -it --restart=Never --image=nvidia/cuda:12.8.1-base-ubuntu24.04 \
    --overrides='{"spec":{"containers":[{"name":"smi","image":"nvidia/cuda:12.8.1-base-ubuntu24.04","command":["nvidia-smi"],"resources":{"limits":{"nvidia.com/gpu":1}}}]}}'
```

The pod sees exactly one GPU. Run it with `"nvidia.com/gpu":2` and it sees both. The device plugin chose which physical GPU each pod got; `kubectl describe pod` won't say, but `nvidia-smi` inside shows the bus ID.

### B4. DCGM dashboard

Print Grafana's admin password from its secret, then forward Grafana's port to the laptop. The port-forward runs in the foreground, so leave that terminal open and browse to `http://localhost:3001`.

**On the laptop (WSL2):**

```bash
kubectl -n monitoring get secret kps-grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
kubectl -n monitoring port-forward svc/kps-grafana 3001:80
```

Sign in as `admin` with the printed password. In Grafana: Dashboards → New → Import → ID **12239** (NVIDIA DCGM Exporter Dashboard) → select the Prometheus data source.

Useful raw metrics for your own panels:

| Metric | Meaning | On a GeForce card |
|---|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | % of time a kernel was running | Available |
| `DCGM_FI_DEV_FB_USED` | GPU memory used (MiB) | Available |
| `DCGM_FI_DEV_GPU_TEMP` | Temperature (°C) | Available |
| `DCGM_FI_DEV_POWER_USAGE` | Power draw (W) | Available |
| `DCGM_FI_DEV_SM_CLOCK` | SM clock (MHz); drops when throttling | Available |
| `DCGM_FI_DEV_XID_ERRORS` | Last Xid error code | Available |
| `DCGM_FI_PROF_SM_ACTIVE`, `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | Real compute and tensor-core activity | **Not available** (data-centre GPUs only) |

The last row is the one you'd use on the p5 fleet to tell busy from productive. Note its absence here; it's an honest limit of the lab.

### B5. Kueue for two teams on two GPUs

Install Kueue as in [stage 1 B6](stage-1-kueue-simulated-fleet.md#b6-kueue) (Helm plus `prometheus.yaml`). Then write a smaller version of the same design, with one flavor matching the GFD label.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/k8s/kueue-home.yaml`

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata: {name: rtx3070}
spec:
  nodeLabels:
    nvidia.com/gpu.product: NVIDIA-GeForce-RTX-3070
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata: {name: team-a}
spec:
  namespaceSelector: {}
  cohortName: home
  preemption: {reclaimWithinCohort: Any, withinClusterQueue: LowerPriority}
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: rtx3070
      resources:
      - {name: cpu, nominalQuota: 6}          # set to about half your CPU threads
      - {name: memory, nominalQuota: 24Gi}    # about half your RAM
      - {name: nvidia.com/gpu, nominalQuota: 1, borrowingLimit: 1}
---
# team-b: identical, with name team-b
```

Add the `team-b` ClusterQueue, namespaces `team-a` and `team-b`, a LocalQueue named `research` in each, and the `batch` and `deadline` WorkloadPriorityClasses from stage 1. Keep the namespaces above the LocalQueues in the file, because `kubectl apply` creates objects in order and a LocalQueue needs its namespace to exist. Then apply the file.

**On the laptop (WSL2):**

```bash
cd ~/gpu-fleet-lab/k8s
kubectl apply -f kueue-home.yaml
kubectl get clusterqueues,localqueues -A
```

Both ClusterQueues and both LocalQueues should be listed. A ClusterQueue that reports itself inactive usually names a flavor that doesn't exist or doesn't match the node's GFD label.

### B6. Code and checkpoint storage

Put the shared scripts in a ConfigMap and give each team a checkpoint volume. k3s ships a `local-path` storage class that creates volumes as directories on the node. The commands run from `~/gpu-fleet-lab/k8s` and expect the two scripts from this repository's `code/` folder copied into `~/gpu-fleet-lab/k8s/code/`.

**On the laptop (WSL2):**

```bash
for ns in team-a team-b; do
    kubectl -n $ns create configmap lab-code --from-file=code/train_ddp.py --from-file=code/preflight.py
    kubectl -n $ns apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: ckpt}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: local-path
  resources: {requests: {storage: 5Gi}}
EOF
  done
```

Each team namespace now has a `lab-code` ConfigMap holding the two scripts and a 5 GiB `ckpt` claim; the claim's YAML is applied inline, so no file is saved. `kubectl get pvc -A` shows the claims as `Pending` until a pod mounts them, at which point `local-path` creates the directory.

Save a training Job template for the exercises. It runs `train_ddp.py` on one GPU, submits to the `research` LocalQueue at `batch` priority, and mounts the code, the checkpoint claim and a RAM-backed `/dev/shm`.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/k8s/train-job.yaml`

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: train
  labels:
    kueue.x-k8s.io/queue-name: research
    kueue.x-k8s.io/priority-class: batch
spec:
  backoffLimit: 3                 # how many pod failures before the Job gives up
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: trainer
        image: pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
        command: ["torchrun", "--nproc-per-node=1", "/code/train_ddp.py",
                  "--steps", "5000", "--ckpt-dir", "/ckpt/train", "--ckpt-every", "200"]
        env:
        - {name: NCCL_DEBUG, value: INFO}
        resources:
          requests: {cpu: "4", memory: 12Gi, nvidia.com/gpu: "1"}
          limits: {nvidia.com/gpu: "1"}
        volumeMounts:
        - {name: code, mountPath: /code}
        - {name: ckpt, mountPath: /ckpt}
        - {name: dshm, mountPath: /dev/shm}
      volumes:
      - name: code
        configMap: {name: lab-code}
      - name: ckpt
        persistentVolumeClaim: {claimName: ckpt}
      - name: dshm                 # Kubernetes' answer to Docker's --shm-size
        emptyDir: {medium: Memory, sizeLimit: 8Gi}
```

The `dshm` volume replaces the tiny default `/dev/shm` with a RAM-backed one; without it PyTorch data-loader workers crash with "bus error", the same problem `shm_size` solved in dstack.

The first run pulls the multi-gigabyte PyTorch image; later runs start quickly because the image is cached on the node. At fleet scale, first-pull time on 32 fresh nodes is a real delay.

---

## Exercises

### E1. Quotas and borrowing on real GPUs

From `~/gpu-fleet-lab/k8s`, submit two copies of the training Job to team-a. The `sed` renames the second copy so both can exist, and `kubectl get workloads` shows how Kueue admitted them.

**On the laptop (WSL2):**

```bash
kubectl -n team-a create -f train-job.yaml
sed 's/name: train$/name: train-2/' train-job.yaml | kubectl -n team-a create -f -
kubectl get workloads -A
```

team-a's second job borrows team-b's idle GPU. Submit a job in team-b and watch it reclaim that GPU. Check the preempted job's log afterwards: when it's readmitted, it resumes from its last checkpoint. Everything from stage 1 E3–E4, now with real processes being killed.

### E2. Two GPUs, one job

Clear the queues, then change the template for one 2-GPU job: `--nproc-per-node=2`, `nvidia.com/gpu: "2"` in requests and limits, and raise team-a's `borrowingLimit` if needed so it can hold both GPUs.

Read the start of the log carefully. NCCL, with `NCCL_DEBUG=INFO`, prints lines like:

```
NCCL INFO Channel 00 : 0[0] -> 1[1] via SHM/direct/direct
NCCL INFO Connected all rings
```

`via SHM` means GPU 0's data goes to host memory and back to GPU 1. On a p5.48xlarge the same line says `via P2P/CUMEM` or `via NVLS` (NVLink SHARP through NVSwitch).

Then measure scaling:

| Run | Throughput (img/s, from the log) |
|---|---|
| 1 GPU | |
| 2 GPUs | |
| Scaling efficiency = 2-GPU ÷ (2 × 1-GPU) | |

Efficiency below 100% is the cost of the all-reduce. Try `--batch-size 32` and `256`: with small batches the GPUs compute briefly and spend proportionally more time communicating, so efficiency drops. That relationship (compute per step vs. communication per step) is why interconnect bandwidth matters so much on the p5 fleet.

Also run the preflight check with the same shape. Copy `train-job.yaml` to `preflight-job.yaml`, change `metadata.name` to `preflight` and request 2 GPUs. The snippet below replaces the container's `command` line in the new file.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/k8s/preflight-job.yaml`

```yaml
        command: ["torchrun", "--nproc-per-node=2", "/code/preflight.py"]
```

Submit the Job from the laptop with `kubectl -n team-a create -f preflight-job.yaml` and read its log with `kubectl -n team-a logs job/preflight`.

It prints the all-reduce bus bandwidth. Note the figure: a few GB/s through host memory. An 8-GPU H100 node measures in the hundreds of GB/s.

### E3. Crash and resume

Start the 1-GPU training job. After a couple of checkpoints, delete its pod and watch the Job controller replace it.

**On the laptop (WSL2):**

```bash
kubectl -n team-a delete pod -l job-name=train
kubectl -n team-a get pods -w
```

**What to notice:** the Job controller creates a replacement pod (counting against `backoffLimit`), which logs `resumed from /ckpt/train/step-N.pt`. Steps since the last checkpoint are redone. Work out the cost: steps lost × time per step × GPUs. On a 32-node p5 job, 10 minutes lost costs 256 GPU × 10 min ≈ 43 GPU-hours.

For multi-pod jobs, Kubernetes' `podFailurePolicy` lets you distinguish a node failure (retry) from a code bug (fail fast). Read about it; it's how you stop a broken job burning retries.

### E4. A hung rank

Start the 2-GPU job with a short NCCL timeout: add `"--nccl-timeout-s", "90"` to the command. Once it's logging steps, open a shell inside the training pod. The pod runs on the GPU node, and `kubectl exec` connects you to it from the laptop.

**On the laptop (WSL2):**

```bash
kubectl -n team-a exec -it <pod> -- bash
```

The prompt changes to `root@<pod-name>`, which means you are inside the pod. Find the two worker processes and freeze one of them.

**Inside the pod:**

```bash
ps -ef | grep train_ddp       # two worker processes, one per GPU (plus torchrun itself)
# if ps is missing from the image:
for p in /proc/[0-9]*; do echo "${p#/proc/} $(tr '\0' ' ' < $p/cmdline)"; done | grep train_ddp
kill -STOP <pid of one worker>
```

`SIGSTOP` freezes the process without killing it, the way a GPU fault or a stuck storage read can freeze a rank.

**What to notice, in order:**

1. The log stops. Nothing errors.
2. In Grafana, the *other* GPU's utilization stays **high**. NCCL's all-reduce kernel is spinning, waiting for the frozen rank. By the basic utilization metric this GPU looks busy while doing no useful work.
3. After ~90 seconds, PyTorch's NCCL watchdog reports a collective timeout and tears the process group down; torchrun ends the job and the pod fails.

Point 2 matters more than it looks: utilization can look healthy during a hang, so hang detection has to watch progress (steps completed, log output) rather than GPU busyness. The default NCCL timeout in PyTorch is 10 minutes; on 256 GPUs that's about 43 GPU-hours wasted per hang before anyone is told. Choosing the timeout is a trade-off against slow-but-legitimate operations such as checkpoint writes.

Resume the process (`kill -CONT`) before the timeout on a second run to see the job recover.

### E5. Time-slicing

Advertise each GPU as four by giving the device plugin a time-slicing config and pointing the GPU Operator's cluster policy at it. The ConfigMap is applied inline, so no file is saved.

**On the laptop (WSL2):**

```bash
kubectl -n gpu-operator apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata: {name: time-slicing-config}
data:
  any: |-
    version: v1
    flags:
      migStrategy: none
    sharing:
      timeSlicing:
        renameByDefault: false
        failRequestsGreaterThanOne: false
        resources:
          - name: nvidia.com/gpu
            replicas: 4
EOF
kubectl patch clusterpolicies.nvidia.com/cluster-policy -n gpu-operator --type merge \
    -p '{"spec": {"devicePlugin": {"config": {"name": "time-slicing-config", "default": "any"}}}}'
kubectl describe node gpu-node | grep nvidia.com/gpu
```

The node now reports `nvidia.com/gpu: 8`, and GFD adds a label marking the GPUs as shared.

Raise the Kueue quotas to match (Kueue counts what the node advertises), then run four 1-"GPU" training jobs with `--batch-size 64`. Watch throughput per job and `DCGM_FI_DEV_FB_USED`. Then raise `--batch-size` until one of them fails with CUDA out-of-memory.

**What to notice:** four jobs share each card's 8 GB with no memory isolation. Each job's throughput drops to roughly a quarter. Time-slicing suits notebooks and debugging, where GPUs are otherwise idle most of the time, and is a poor fit for training. On a p5 fleet, dstack's `blocks` (whole GPUs per job) and MIG (hardware partitions on H100) are the alternatives.

Revert afterwards: patch the cluster policy back with `"config": {"name": "", "default": ""}` or delete the ConfigMap and restart the device plugin pods.

### E6. Thermal behaviour over a long run

Run the 2-GPU job for an hour with a batch size that nearly fills memory. Watch temperature, power and SM clock in Grafana.

**What to notice:** gaming loads vary; training holds the card at its power limit continuously. If the top card (with the second card blowing into it) shows falling SM clocks as temperature rises, that's thermal throttling, and the job's throughput will drift down with it. Screenshot the panels: this is your "healthy baseline" for comparing future runs, which is exactly what a fleet operator keeps per node type.

### E7. Take the node out of service

Cordon the node, submit a Job, and look at its pod before uncordoning.

**On the laptop (WSL2):**

```bash
kubectl cordon gpu-node
kubectl -n team-a create -f train-job.yaml     # admitted by Kueue, but the pod stays Pending
kubectl get pods -A -o wide | grep -v Running
kubectl uncordon gpu-node
```

`cordon` stops new pods landing on a node without touching running ones. `drain` also evicts what's running. On a single-node cluster draining would evict CoreDNS and everything else too, so here you cordon and delete the GPU pods yourself.

**What to notice:** Kueue admitted the job even though no node could take it. Quota and physical capacity are separate. On a real fleet, a node taken out for repair should also come out of the quota (or `waitForPodsReady` should catch the stuck admission), otherwise the queue promises GPUs that don't exist. Note how you'd handle that; it belongs in the failure runbook.

### E8. dstack on top of Kueue

This is the experiment the reference fleet's evaluation needs: can researchers keep using dstack while Kueue enforces quotas underneath?

**How it could work.** dstack's Kubernetes backend creates plain pods, not Jobs. Kueue manages plain pods through its `pod` integration (enabled by default in current releases): it adds a `kueue.x-k8s.io/admission` *scheduling gate* so the kube-scheduler ignores the pod until Kueue admits it, then removes the gate. Kueue normally expects a `kueue.x-k8s.io/queue-name` label on the pod; dstack won't add one, so the pods need another route to a queue.

**Set-up:**

1. Run one dstack task on the Kubernetes backend (step 3) without Kueue involvement and note which namespace dstack creates pods in and what labels they carry.
2. In the `kueue-manager-config` ConfigMap (extract, edit and reload it the way [stage 1 B6](stage-1-kueue-simulated-fleet.md#b6-kueue) does), look at `managedJobsNamespaceSelector` (which namespaces Kueue manages) and `manageJobsWithoutQueueName`. Point the selector at dstack's namespace only, so Kueue doesn't start gating system pods. Then check your Kueue version's documentation for **LocalQueue defaulting**, which sends unlabelled workloads in a namespace to a LocalQueue named `default`. If available, create a LocalQueue named `default` in dstack's namespace pointing at `team-a`. Restart the controller.
3. Add a Kubernetes backend to `~/.dstack/server/config.yml` (see the Kubernetes backend reference: `kubeconfig` pointing at `~/.kube/home.yaml`, and `proxy_jump` settings so dstack can reach pods through the node). Restart the server and run the training task from stage 2B against it.

**Hypothesis to test:** dstack's pod gets a scheduling gate, Kueue admits it within team-a's quota, and the task runs. Then fill team-a's quota and submit another dstack task:

| Observation | Meaning |
|---|---|
| dstack task waits, then runs when quota frees | Integration works for admission |
| dstack reports a failure or timeout while the pod is gated | dstack expects pods to start promptly; would need its `retry` policy or a change upstream |
| Kueue preempts a dstack pod and dstack retries it | Preemption works end to end |
| Kueue never sees the pod | Namespace selector or queue routing is wrong, or your Kueue version can't route unlabelled pods — in which case dstack would need to label its pods, which is itself a finding |

Whatever you observe, write it up; a tested answer to "can we keep dstack and add Kueue?" is more useful to a team making that decision than any opinion.

---

## Record

In `gpu-fleet-lab/notes/k8s-gpu.md`:

1. Your E2 table and NCCL transport line, beside what a p5.48xlarge would report.
2. How long did the E4 hang take to detect? What would you set the NCCL timeout to on a 256-GPU fleet, and what else would you monitor to catch hangs sooner?
3. When would you allow time-slicing on a research fleet?
4. What did E7 show about the gap between quota and real capacity?
5. The E8 result, with evidence.

## Clean up

k3s can stay for later stages. When you want it gone, run the uninstall script the k3s installer left behind.

**On the GPU node:**

```bash
sudo /usr/local/bin/k3s-uninstall.sh
```

The script removes k3s, its containerd and all cluster data. Docker and the NVIDIA toolkit are unaffected, so dstack and the Slurm extension keep working.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Node shows no `nvidia.com/gpu` | Device plugin not running, or k3s didn't pick up the nvidia runtime | Check `gpu-operator` pods; confirm the containerd config grep in B1; restart k3s after installing the toolkit |
| Operator validator pods crash-looping | Driver or toolkit mismatch with the flags | With host driver and toolkit, both `driver.enabled` and `toolkit.enabled` must be `false` |
| GPU pod: `Failed to initialize NVML` | Pod didn't run under the nvidia runtime | Confirm `default-runtime: nvidia`, or set `runtimeClassName: nvidia` in the pod |
| Helm install fails on ServiceMonitor | kube-prometheus-stack not installed yet | Install B2 first |
| DCGM dashboard empty | Prometheus not scraping the exporter | Check Prometheus → Targets for `nvidia-dcgm-exporter` |
| Job admitted, pod Pending | Node cordoned, or requests exceed what the node has free | `kubectl describe pod` shows the scheduler's reason |
| NCCL error: `unhandled system error` at start | Often too little `/dev/shm` | Mount the `dshm` volume |

## References

- [K3s — Advanced options: alternative container runtimes](https://docs.k3s.io/advanced)
- [NVIDIA GPU Operator — Getting started](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/getting-started.html)
- [NVIDIA GPU Operator — Time-slicing](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html)
- [NVIDIA — DCGM exporter metrics](https://docs.nvidia.com/datacenter/dcgm/latest/reference/dcgm-exporter-metrics.html)
- [dcgm-exporter issue #506 — profiling metrics on GeForce](https://github.com/NVIDIA/dcgm-exporter/issues/506)
- [Kueue — Run plain pods](https://kueue.sigs.k8s.io/docs/tasks/run/plain_pods/)
- [dstack — server config reference (Kubernetes backend)](https://dstack.ai/docs/reference/server/config.yml/)
- [Kubernetes — Pod failure policy](https://kubernetes.io/docs/tasks/job/pod-failure-policy/)
