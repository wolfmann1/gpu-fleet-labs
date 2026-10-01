# Stage 1 — Kueue on a simulated 256-GPU fleet

[Labs index](README.md) · Previous: [Stage 2B — dstack](stage-2b-dstack.md) · Next: [Stage 2 — Kubernetes GPU node](stage-2-k8s-gpu-node.md)

## Why this lab

The [reference fleet](scenario.md) is evaluating Kubernetes with Kueue to add what dstack lacks: team quotas, borrowing, preemption and all-or-nothing admission. You can't rent 256 H100s to learn it, but Kueue's decisions only depend on what the cluster *reports*. This lab builds a Kubernetes cluster on the laptop with 32 fake p5.48xlarge nodes and 4 fake p5.4xlarge singles, matching the reference fleet, and exercises Kueue's policies against them.

By the end you will have:

- a kind cluster with a simulated 260-GPU fleet;
- Kueue configured for two research teams sharing the fleet, plus a development queue;
- watched borrowing, reclaim, priority preemption and gang admission happen;
- a Grafana panel showing queue depth per team.

## Where things run

Everything in this lab runs **on the laptop, in WSL2**: the kind cluster, all `kubectl` and `helm` commands, and the scripts and YAML files, kept in `~/gpu-fleet-lab/kueue-sim`. The GPU node isn't used.

## Concepts

### Kubernetes in Docker terms

| Kubernetes | Nearest Docker idea | Notes |
|---|---|---|
| Pod | A container (or a few sharing a network) | The unit that gets scheduled onto a node |
| Node | A Docker host | Reports its capacity: CPU, memory, and extended resources such as `nvidia.com/gpu` |
| Job | `docker run` for a batch task, repeated N times | `parallelism` pods run at once; the Job is done when `completions` pods succeed |
| Namespace | — | A folder for objects; used here as one per team |
| Label | Docker label | Key/value tags; selectors use them to match objects |
| Taint / toleration | — | A taint on a node repels pods; only pods with a matching toleration may land there |
| kube-scheduler | — | Places each pod on a node with enough free resources, one pod at a time |
| Controller | — | A loop that watches objects and acts to make reality match them |

### What Kueue adds

The kube-scheduler places *pods*. It knows nothing about teams, and it places pods one at a time, so a 16-pod training job can end up with 12 pods running and 4 pending, holding 96 GPUs that do nothing. Kueue works one level up, on whole *jobs*:

1. A Job is created with a label naming a queue. Kueue's webhook creates it **suspended**, so no pods exist yet.
2. Kueue represents it as a **Workload**: the sum of all its pods' resource requests.
3. Kueue decides whether the Workload fits the team's quota (plus anything it may borrow). If it fits as a whole, Kueue **admits** it: it unsuspends the Job and adds node selectors and tolerations for the chosen flavor.
4. The kube-scheduler then places the pods as usual.
5. To preempt, Kueue suspends the Job again; its pods are deleted and the Workload goes back in the queue.

Kueue never places pods; it controls when a job is allowed to create them. That's why it layers onto existing Kubernetes rather than replacing the scheduler.

### Kueue's objects

```
ResourceFlavor  "h100-8x"  ── nodeLabels: instance-type=p5.48xlarge
ResourceFlavor  "h100-1x"  ── nodeLabels: instance-type=p5.4xlarge

Cohort "research"  (a pool whose members lend and borrow unused quota)
 ├── ClusterQueue "team-a"   nominalQuota: 128 GPUs of h100-8x
 └── ClusterQueue "team-b"   nominalQuota: 128 GPUs of h100-8x
ClusterQueue "dev"           nominalQuota: 4 GPUs of h100-1x   (no cohort)

LocalQueue team-a/research  ──► ClusterQueue team-a
LocalQueue team-b/research  ──► ClusterQueue team-b
LocalQueue dev/dev          ──► ClusterQueue dev
```

| Object | Scope | Purpose |
|---|---|---|
| ResourceFlavor | Cluster | A kind of capacity, identified by node labels. One per GPU type or instance type |
| ClusterQueue | Cluster | A quota: how much of each flavor a team is guaranteed (`nominalQuota`), may borrow (`borrowingLimit`), and its preemption rules |
| Cohort | Named in ClusterQueues (`cohortName`) | Groups ClusterQueues that share unused quota |
| LocalQueue | Namespace | Where users submit. Points to one ClusterQueue, so admins can change quotas without touching users' manifests |
| WorkloadPriorityClass | Cluster | A named priority value for jobs, independent of pod priority |

### What kwok does

kwok ("Kubernetes WithOut Kubelet") creates Node objects that no real machine backs. Its controller answers for them: it marks them Ready and moves pods scheduled onto them through their lifecycle stages. The control plane, kube-scheduler and Kueue are all real; only the machines are imaginary. A fake node can advertise `nvidia.com/gpu: 8` and the scheduler will believe it.

---

## Build

### B1. Tools (WSL2 on the laptop)

From [prerequisites §2](00-prerequisites.md#2-laptop-wsl2-ubuntu-2404): Docker for kind (§2.1), base packages including `jq` (§2.2), kubectl (§2.4), Helm (§2.5) and kind (§2.6).

**Check, on the laptop (WSL2):** `docker run --rm hello-world && kind version && kubectl version --client && helm version`.

Create the working folder for this lab. The scripts and YAML files from B4 onward are saved here, and later commands expect to run from it.

**On the laptop (WSL2):**

```bash
mkdir -p ~/gpu-fleet-lab/kueue-sim && cd ~/gpu-fleet-lab/kueue-sim
```

The folder exists and is your current directory. When you open a new terminal for this lab, `cd ~/gpu-fleet-lab/kueue-sim` first.

### B2. The cluster

Create a kind cluster named `fleet-sim` and list its nodes.

**On the laptop (WSL2):**

```bash
kind create cluster --name fleet-sim
kubectl get nodes
```

One real node, `fleet-sim-control-plane`, running in a Docker container. It runs the control plane and will host Kueue, Prometheus and Grafana.

### B3. kwok

Install the kwok controller and its `stage-fast` stage set from the latest GitHub release.

**On the laptop (WSL2):**

```bash
KWOK_REPO=kubernetes-sigs/kwok
KWOK_LATEST=$(curl -s "https://api.github.com/repos/${KWOK_REPO}/releases/latest" | jq -r '.tag_name')
kubectl apply -f "https://github.com/${KWOK_REPO}/releases/download/${KWOK_LATEST}/kwok.yaml"
kubectl apply -f "https://github.com/${KWOK_REPO}/releases/download/${KWOK_LATEST}/stage-fast.yaml"
kubectl get stages
```

`kubectl get stages` lists the lifecycle stages the kwok controller now applies to pods on fake nodes.

`stage-fast` makes pods on fake nodes become Running at once, and makes Job-owned pods **complete** a second later. For this lab jobs need to keep running until you remove them or Kueue preempts them, so delete the completion stage.

**On the laptop (WSL2):**

```bash
kubectl delete stage pod-complete
```

Now a simulated training job runs until something stops it, which is closer to a multi-day run.

### B4. The fake fleet

`make-fleet.sh` generates the reference fleet's shape: 32 eight-GPU nodes and 4 single-GPU nodes. Create the file in your working folder with the contents below.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/kueue-sim/make-fleet.sh`

```bash
#!/usr/bin/env bash
# Usage: ./make-fleet.sh | kubectl apply -f -
node() {  # name instance-type gpus cpus memory
cat <<EOF
---
apiVersion: v1
kind: Node
metadata:
  name: $1
  annotations:
    kwok.x-k8s.io/node: fake
    node.alpha.kubernetes.io/ttl: "0"
  labels:
    kubernetes.io/hostname: $1
    kubernetes.io/os: linux
    kubernetes.io/arch: amd64
    type: kwok
    instance-type: $2
    topology.kubernetes.io/zone: us-east-2a
spec:
  taints:
  - key: kwok.x-k8s.io/node
    value: fake
    effect: NoSchedule
status:
  allocatable: {cpu: "$4", memory: $5, pods: "110", nvidia.com/gpu: "$3"}
  capacity:    {cpu: "$4", memory: $5, pods: "110", nvidia.com/gpu: "$3"}
EOF
}
for i in $(seq -w 0 31); do node "p5-48xl-$i" p5.48xlarge 8 192 2Ti; done
for i in $(seq 0 3);      do node "p5-4xl-$i"  p5.4xlarge  1 16  256Gi; done
```

The script writes one Node manifest per fake machine to standard output, ready to pipe into `kubectl apply`.

Make the script executable, apply its output, and confirm the nodes report an instance type and a GPU count.

**On the laptop (WSL2):**

```bash
chmod +x make-fleet.sh && ./make-fleet.sh | kubectl apply -f -
kubectl get nodes -L instance-type
kubectl get nodes -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu' | head
```

The node list should show 36 kwok nodes beside the control plane, and the GPU column should read 8 for each `p5-48xl` node.

**Why the taint:** without it, the scheduler would also put system pods (Prometheus, Kueue) on fake nodes, where nothing would really run. The taint keeps everything off them except pods that tolerate it, and Kueue will add that toleration for workloads it admits.

### B5. Prometheus and Grafana

Install kube-prometheus-stack into a `monitoring` namespace. It collects Kueue's metrics and hosts the Grafana dashboard you build in E7.

**On the laptop (WSL2):**

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm repo update
helm install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
```

That `--set` matters: by default this chart's Prometheus only scrapes ServiceMonitors created by its own Helm release. Setting it to `false` makes it pick up the ServiceMonitor Kueue installs next. Forgetting this is the classic "the exporter works but Grafana shows nothing".

### B6. Kueue

Install Kueue 0.19.6 with Helm, then apply Kueue's Prometheus manifest so the stack from B5 scrapes its metrics.

**On the laptop (WSL2):**

```bash
helm install kueue oci://registry.k8s.io/kueue/charts/kueue --version=0.19.6 \
    -n kueue-system --create-namespace --wait --timeout 300s
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.19.6/prometheus.yaml
kubectl -n kueue-system get pods
```

The last command should show a `kueue-controller-manager-…` pod as Running before you continue.

**All-or-nothing with ready pods.** Kueue admits a Job as a whole, but by default it considers the job started once admitted, even if some pods can't actually be scheduled (on a real cluster, a node might be missing or unhealthy). `waitForPodsReady` makes Kueue wait until every pod is Ready and evict and requeue the workload if that doesn't happen within a timeout. The setting lives in the controller's configuration, which is stored as a YAML document inside the `kueue-manager-config` ConfigMap, under the key `controller_manager_config.yaml`. Editing it in place with `kubectl edit` is error-prone: the document is nested inside the ConfigMap, so every line carries four extra spaces of indentation, and a single misaligned line makes the edit fail to save. Extracting the document to a file, editing that, and loading it back avoids the nesting.

Extract the controller configuration to a file in your working folder.

**On the laptop (WSL2):**

```bash
cd ~/gpu-fleet-lab/kueue-sim
kubectl -n kueue-system get configmap kueue-manager-config \
    -o jsonpath='{.data.controller_manager_config\.yaml}' > kueue-config.yaml
grep -n "waitForPodsReady" kueue-config.yaml
```

`kueue-config.yaml` now holds the controller configuration with no ConfigMap wrapping, so its top-level keys (`apiVersion`, `kind`, `controller`, `integrations`, …) start at the left margin. With the Helm install used here, `grep` prints nothing: the chart parses the configuration and writes it back out, which drops the comments, so there is no `waitForPodsReady` block yet. If you installed Kueue from the release manifest instead, `grep` shows a commented-out `#waitForPodsReady:` block; delete that block and its indented `#` lines in an editor before the next step.

Append the setting to the end of the file. The command writes the three lines exactly as shown, so indentation doesn't depend on an editor: `waitForPodsReady:` lands at the left margin as a top-level key, and the two lines under it are indented by two spaces.

**On the laptop (WSL2):**

```bash
cat >> kueue-config.yaml <<'EOF'
waitForPodsReady:
  timeout: 5m
  blockAdmission: true
EOF
tail -4 kueue-config.yaml
```

`tail` shows the last lines of the file, ending with the new block. Paste the whole block at once, as with any here-document: the shell keeps reading until the line that contains only `EOF`. Run the append once. If you run it twice by mistake, rerun the extract step above to start again from a clean copy.

Before loading the file, check it's valid YAML and that the new block sits where you expect.

**On the laptop (WSL2):**

```bash
python3 -c "import yaml,sys; c=yaml.safe_load(open('kueue-config.yaml')); print(c['waitForPodsReady'])"
```

The command prints `{'timeout': '5m', 'blockAdmission': True}`. An error naming a line and column means the indentation is off at that line; a `KeyError` means `waitForPodsReady` isn't at the top level. If Python reports `No module named 'yaml'`, install it with `sudo apt install -y python3-yaml` and run the check again.

Load the edited file back into the ConfigMap. `--dry-run=client -o yaml` builds the ConfigMap locally from the file, and `kubectl apply` replaces the one in the cluster.

**On the laptop (WSL2):**

```bash
kubectl -n kueue-system create configmap kueue-manager-config \
    --from-file=controller_manager_config.yaml=kueue-config.yaml \
    --dry-run=client -o yaml | kubectl apply -f -
```

kubectl may warn that the ConfigMap is missing a `last-applied-configuration` annotation; that's expected for an object Helm created, and the change is applied. A later `helm upgrade` of Kueue would restore the chart's default configuration, so keep `kueue-config.yaml` to reapply.

The controller reads its configuration only at start-up, so restart it.

**On the laptop (WSL2):**

```bash
kubectl -n kueue-system rollout restart deployment kueue-controller-manager
kubectl -n kueue-system rollout status deployment kueue-controller-manager
```

The second command returns once the new controller pod is running with the updated setting. `blockAdmission: true` admits one workload at a time until its pods are ready, which prevents two large jobs from each getting half their pods and deadlocking. The trade-off is slower admission when many jobs are waiting.

### B7. Flavors, queues and teams

`kueue-setup.yaml` defines the two flavors, the three ClusterQueues, the team namespaces with their LocalQueues, and two priority classes. Create the file in your working folder with the contents below; B7's apply step reads it from there.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/kueue-sim/kueue-setup.yaml`

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: h100-8x
spec:
  nodeLabels:
    instance-type: p5.48xlarge
  tolerations:                      # added to admitted pods so they may use the tainted nodes
  - key: kwok.x-k8s.io/node
    operator: Equal
    value: fake
    effect: NoSchedule
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: h100-1x
spec:
  nodeLabels:
    instance-type: p5.4xlarge
  tolerations:
  - key: kwok.x-k8s.io/node
    operator: Equal
    value: fake
    effect: NoSchedule
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: team-a
spec:
  namespaceSelector: {}
  cohortName: research
  queueingStrategy: BestEffortFIFO
  preemption:
    reclaimWithinCohort: Any            # take back lent quota from anyone in the cohort
    withinClusterQueue: LowerPriority   # higher-priority team jobs may evict lower ones
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: h100-8x
      resources:
      - name: cpu
        nominalQuota: 3072
      - name: memory
        nominalQuota: 32Ti
      - name: nvidia.com/gpu
        nominalQuota: 128               # 16 nodes
        borrowingLimit: 128             # may borrow up to all of team-b's share when idle
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: team-b
spec:
  namespaceSelector: {}
  cohortName: research
  queueingStrategy: BestEffortFIFO
  preemption:
    reclaimWithinCohort: Any
    withinClusterQueue: LowerPriority
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: h100-8x
      resources:
      - name: cpu
        nominalQuota: 3072
      - name: memory
        nominalQuota: 32Ti
      - name: nvidia.com/gpu
        nominalQuota: 128
        borrowingLimit: 128
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: dev
spec:
  namespaceSelector: {}
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: h100-1x
      resources:
      - name: cpu
        nominalQuota: 64
      - name: memory
        nominalQuota: 1Ti
      - name: nvidia.com/gpu
        nominalQuota: 4
---
apiVersion: v1
kind: Namespace
metadata: {name: team-a}
---
apiVersion: v1
kind: Namespace
metadata: {name: team-b}
---
apiVersion: v1
kind: Namespace
metadata: {name: dev}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata: {name: research, namespace: team-a}
spec: {clusterQueue: team-a}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata: {name: research, namespace: team-b}
spec: {clusterQueue: team-b}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata: {name: dev, namespace: dev}
spec: {clusterQueue: dev}
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata: {name: batch}
value: 100
description: "Default for long-running research jobs"
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: WorkloadPriorityClass
metadata: {name: deadline}
value: 1000
description: "Paper or milestone deadlines, approved by research leads"
```

The inline comments mark the fields that control sharing: the flavor tolerations, the preemption rules and the borrowing limit.

Apply the file and list the queues it creates.

**On the laptop (WSL2):**

```bash
kubectl apply -f kueue-setup.yaml
kubectl get clusterqueues,localqueues -A
```

Read the ClusterQueue spec before moving on. Every design decision about sharing the fleet is in those few fields: how much each team is guaranteed, how much it may borrow, whether lent capacity comes back by force, and whether priority can evict a running job.

### B8. A job template

`job.sh` submits a simulated training job to a Kueue queue. Create the file in your working folder with the contents below.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/kueue-sim/job.sh`

```bash
#!/usr/bin/env bash
# Usage: ./job.sh <namespace> <queue> <name> <nodes> [priority-class]
NS=$1 Q=$2 NAME=$3 NODES=$4 PRIO=${5:-batch}
cat <<EOF | kubectl create -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: $NAME
  namespace: $NS
  labels:
    kueue.x-k8s.io/queue-name: $Q
    kueue.x-k8s.io/priority-class: $PRIO
spec:
  parallelism: $NODES
  completions: $NODES
  completionMode: Indexed
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: trainer
        image: registry.k8s.io/pause:3.10   # never actually runs; kwok simulates it
        resources:
          requests: {cpu: "96", memory: 1Ti, nvidia.com/gpu: "8"}
          limits:   {nvidia.com/gpu: "8"}
EOF
```

Each pod asks for a whole p5 node. A 4-node job is a 32-GPU training run.

A new file isn't executable, so mark the script as a program before the exercises call it as `./job.sh`.

**On the laptop (WSL2):**

```bash
chmod +x job.sh
ls -l job.sh
```

The permissions in the `ls` output now include `x`, as in `-rwxr-xr-x`. Without this step, `./job.sh` fails with `Permission denied`.

---

## Exercises

Open two terminals and run one of these views in each, so you can watch Kueue react during the exercises.

**On the laptop (WSL2):**

```bash
watch -n2 kubectl get workloads -A
watch -n2 'kubectl get clusterqueue -o custom-columns=NAME:.metadata.name,PENDING:.status.pendingWorkloads,ADMITTED:.status.admittedWorkloads'
```

The first view lists every Workload and its admission state; the second shows pending and admitted counts per ClusterQueue.

To see GPU usage and borrowing for a team: `kubectl get clusterqueue team-a -o jsonpath='{.status.flavorsUsage}' | jq`.

### E1. One job, end to end

Submit a one-node job to team-a, then check the Job's `suspend` field, its Workload and the node its pod landed on.

**On the laptop (WSL2):**

```bash
./job.sh team-a research a-small 1
kubectl -n team-a get job a-small -o jsonpath='{.spec.suspend}{"\n"}'
kubectl -n team-a get workloads
kubectl -n team-a get pods -o wide
```

**What to notice:** the Job's `suspend` flipped to `false` after admission. The Workload records which flavor was assigned. `kubectl -n team-a get pod -o yaml` shows a `nodeSelector` for `instance-type: p5.48xlarge` and the kwok toleration, neither of which you wrote: Kueue injected them from the flavor.

### E2. Gang admission

Fill team-a's quota exactly, put 96 GPUs of work on team-b, then ask for more on team-a.

**On the laptop (WSL2):**

```bash
./job.sh team-a research a-big 15      # 15 + the 1 from E1 = 16 nodes = 128 GPUs
./job.sh team-b research b-big 12      # team-b uses 96 of its 128
./job.sh team-a research a-extra 6     # 6 nodes; team-b has only 4 nodes' worth idle
```

**What to notice:** `a-extra` stays **Pending**, with zero pods. It needs 48 GPUs; team-a is at quota and the cohort has 32 unused. Kueue won't admit part of it. `kubectl -n team-a describe workload` gives the reason in plain words.

Now compare with plain Kubernetes, which has no gang admission. Kueue ignores Jobs that carry no queue label, so a second script that builds the same Job without Kueue's labels bypasses it. Kueue also won't add the flavor's node selector and toleration to a Job it ignores, so this script writes them into the pod spec itself. Leave `job.sh` unchanged; the later exercises still use it.

Create `raw-job.sh` in your working folder with the contents below. Compared with `job.sh` it has no `labels:` block, takes no queue or priority argument, and adds `nodeSelector` and `tolerations` under the pod `spec:`.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/kueue-sim/raw-job.sh`

```bash
#!/usr/bin/env bash
# Usage: ./raw-job.sh <namespace> <name> <nodes>
# Same simulated training job as job.sh, without Kueue's labels.
NS=$1 NAME=$2 NODES=$3
cat <<EOF | kubectl create -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: $NAME
  namespace: $NS
spec:
  parallelism: $NODES
  completions: $NODES
  completionMode: Indexed
  template:
    spec:
      restartPolicy: Never
      nodeSelector: {instance-type: p5.48xlarge}
      tolerations:
      - {key: kwok.x-k8s.io/node, operator: Equal, value: fake, effect: NoSchedule}
      containers:
      - name: trainer
        image: registry.k8s.io/pause:3.10   # never actually runs; kwok simulates it
        resources:
          requests: {cpu: "96", memory: 1Ti, nvidia.com/gpu: "8"}
          limits:   {nvidia.com/gpu: "8"}
EOF
```

The script takes three arguments:

| Argument | Example | Meaning |
|---|---|---|
| `<namespace>` | `nokueue` | Namespace to create the Job in; one with no LocalQueue, so nothing routes it to Kueue |
| `<name>` | `raw` | The Job's name |
| `<nodes>` | `6` | Number of pods, each asking for a whole 8-GPU node |

Delete the pending `a-extra` job so it isn't competing for the free nodes, make the new script executable, then submit a 6-node raw job in a new `nokueue` namespace and list its pods.

**On the laptop (WSL2):**

```bash
kubectl -n team-a delete job a-extra
chmod +x raw-job.sh
kubectl create namespace nokueue
./raw-job.sh nokueue raw 6
kubectl -n nokueue get pods -o wide
```

Four pods start on the four free nodes and two stay Pending indefinitely. In a real training job those four would each hold 8 H100s, wait at the NCCL rendezvous for ranks that never arrive, and time out. Delete the namespace when you're done, which also deletes the Job: `kubectl delete namespace nokueue`.

### E3. Borrowing

Remove team-b's job so its share sits idle, then give team-a a 12-node job that fits only by borrowing.

**On the laptop (WSL2):**

```bash
kubectl -n team-b delete job b-big              # team-b goes idle
./job.sh team-a research a-extra 12             # 12 more nodes for team-a
kubectl get clusterqueue team-a -o jsonpath='{.status.flavorsUsage}' | jq
```

**What to notice:** `a-extra` is admitted. team-a now uses 224 GPUs: its own 128 plus 96 **borrowed** from team-b's idle share. The status shows the borrowed amount separately. Idle quota isn't wasted.

### E4. Reclaim

team-b comes back and submits an 8-node job against its own quota.

**On the laptop (WSL2):**

```bash
./job.sh team-b research b-urgent 8
```

**What to notice:** team-b needs 64 GPUs of its guaranteed 128, but only 32 are free. Because team-b's ClusterQueue has `reclaimWithinCohort: Any`, Kueue evicts a team-a workload that is running on borrowed quota. `kubectl -n team-a describe workload` on the victim shows an `Evicted`/`Preempted` condition; its Job is suspended, pods deleted, and it goes back to Pending. It will be readmitted when capacity frees up.

This is the policy that makes lending safe: a team lends idle GPUs knowing it can take them back. It's also where checkpointing stops being optional. A preempted training job loses everything since its last checkpoint, so the preemption policy and the checkpoint interval have to be designed together.

### E5. Priority within a team

Reset team-a to a full quota of `batch` jobs, then submit a deadline job.

**On the laptop (WSL2):**

```bash
kubectl delete jobs --all -n team-a; kubectl delete jobs --all -n team-b
./job.sh team-a research a-batch-1 8 batch
./job.sh team-a research a-batch-2 8 batch
./job.sh team-b research b-fill 16          # occupy team-b so team-a has nothing to borrow
./job.sh team-a research a-deadline 8 deadline
```

**What to notice:** `a-deadline` preempts one of the `batch` jobs (`withinClusterQueue: LowerPriority`). Compare with dstack exercise D4, where the high-priority run waited. Also notice that a deadline job cannot take team-b's guaranteed quota; priority operates inside a team's share, and the cohort rules protect the other team.

Then consider the policy question: who is allowed to use `deadline`? Kueue enforces the rule once written; someone has to decide it. On a shared fleet that's a conversation with research leads, and the answer belongs in the decision record.

### E6. The dev queue

The `dev` ClusterQueue covers the four single-GPU p5.4xlarge nodes, with a quota of 4 GPUs. Start by sending it five jobs from the usual template.

**On the laptop (WSL2):**

```bash
for i in 1 2 3 4 5; do ./job.sh dev dev dev-$i 1 batch; done
kubectl -n dev get workloads
```

All five stay pending. Each pod from `job.sh` asks for 8 GPUs, more than the `dev` queue's whole quota, so Kueue can never admit them; `kubectl -n dev describe workload` gives the reason. Kueue rejects a request that can't fit before it ever reaches a node.

Development work needs single-GPU pods. Delete the five jobs, then create a separate template for them so `job.sh` stays as it is for the other exercises.

**On the laptop (WSL2):**

```bash
kubectl -n dev delete jobs --all
```

`dev-job.sh` is `job.sh` with a smaller pod: one GPU, 8 CPUs and 64 GiB of memory, sized for a p5.4xlarge. Kubernetes requires a GPU request to equal its limit, because GPUs can't be shared or overcommitted, so both are set to 1. Create the file in your working folder with the contents below.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/kueue-sim/dev-job.sh`

```bash
#!/usr/bin/env bash
# Usage: ./dev-job.sh <namespace> <queue> <name> [priority-class]
# One single-GPU pod, sized for a p5.4xlarge development node.
NS=$1 Q=$2 NAME=$3 PRIO=${4:-batch}
cat <<EOF | kubectl create -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: $NAME
  namespace: $NS
  labels:
    kueue.x-k8s.io/queue-name: $Q
    kueue.x-k8s.io/priority-class: $PRIO
spec:
  parallelism: 1
  completions: 1
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: dev
        image: registry.k8s.io/pause:3.10   # never actually runs; kwok simulates it
        resources:
          requests: {cpu: "8", memory: 64Gi, nvidia.com/gpu: "1"}
          limits:   {nvidia.com/gpu: "1"}
EOF
```

`memory: 64Gi` needs its unit: a bare `64` means 64 bytes. Make the script executable and submit five single-GPU jobs to the `dev` queue.

**On the laptop (WSL2):**

```bash
chmod +x dev-job.sh
for i in 1 2 3 4 5; do ./dev-job.sh dev dev dev-$i; done
kubectl -n dev get workloads
kubectl -n dev get pods -o wide
```

**What to notice:** four are admitted onto the four singles; the fifth waits. The `dev` ClusterQueue has no cohort, so it can't borrow from the research teams and they can't borrow from it. Is that what you'd want? Separating interactive work from training capacity is common; the cost is idle singles when nobody is debugging.

### E7. Queue depth in Grafana

This exercise builds a dashboard that shows, per team, how many workloads are waiting, how many are running, how many GPUs each team holds against its quota, and when preemptions happen. It has four parts: turn on Kueue's per-queue resource metrics, open Grafana, build one panel by hand to learn the editor, then import the full dashboard from this repository.

#### Turn on per-queue resource metrics

Kueue always exports its queue counts, but the GPU usage and quota metrics (`kueue_cluster_queue_resource_usage`, `kueue_cluster_queue_nominal_quota`) are off until `metrics.enableClusterQueueResources` is set in the controller configuration. Add it to the `kueue-config.yaml` you extracted in B6. The configuration already has a top-level `metrics:` block, so the new line goes inside that block, indented two spaces; `sed` inserts it directly under `metrics:`.

**On the laptop (WSL2):**

```bash
cd ~/gpu-fleet-lab/kueue-sim
sed -i '/^metrics:/a\  enableClusterQueueResources: true' kueue-config.yaml
python3 -c "import yaml; print(yaml.safe_load(open('kueue-config.yaml'))['metrics'])"
```

The check prints the `metrics` block with both keys, for example `{'bindAddress': ':8443', 'enableClusterQueueResources': True}`. If `enableClusterQueueResources` appears twice in the file, `sed` ran twice; delete the extra line in an editor.

Load the configuration back and restart the controller, as in B6.

**On the laptop (WSL2):**

```bash
kubectl -n kueue-system create configmap kueue-manager-config \
    --from-file=controller_manager_config.yaml=kueue-config.yaml \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl -n kueue-system rollout restart deployment kueue-controller-manager
kubectl -n kueue-system rollout status deployment kueue-controller-manager
```

When the rollout finishes, the controller exports the resource metrics on its next scrape, which Prometheus collects within about 30 seconds.

#### Open Grafana

The Grafana that kube-prometheus-stack installed in B5 runs inside the cluster. Print its admin password, then forward its service to port 3001 on the laptop. The port-forward holds the terminal until you press Ctrl+C, so use a terminal you can leave open.

**On the laptop (WSL2):**

```bash
kubectl -n monitoring get secret kps-grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
kubectl -n monitoring port-forward svc/kps-grafana 3001:80
```

Open `http://localhost:3001` in a Windows browser (WSL2 forwards localhost) and sign in as `admin` with the printed password. The chart already connected Grafana to the cluster's Prometheus as a data source named **Prometheus**.

#### Build one panel by hand

Building a panel yourself shows how a query becomes a graph. This one plots pending workloads per team. **In Grafana, in the browser:**

1. In the left menu choose **Dashboards**, then **New** → **New dashboard**, then **Add visualization**.
2. When asked for a data source, choose **Prometheus**. The panel editor opens with a graph at the top and a query row (**A**) underneath.
3. In the query row, switch the editor from **Builder** to **Code** with the toggle at the row's right-hand side, then paste the query:

   ```
   sum by (cluster_queue) (kueue_pending_workloads)
   ```

   Click **Run queries**. `kueue_pending_workloads` has one series per queue and status; `sum by (cluster_queue)` adds them into one line per team.
4. Under the query, open **Options**, set **Legend** to **Custom** and enter `{{cluster_queue}}`, so each line is labelled with the team's queue name.
5. In the right-hand pane, under **Panel options**, set **Title** to `Pending workloads per team`. Leave the visualization type as **Time series**.
6. Click **Back to dashboard**, then **Save dashboard** (top right), name it `Kueue scratch`, and save.

Set the time range (top right) to **Last 15 minutes** and the refresh to **10s**. With the jobs from the earlier exercises still in place, the `team-a` line sits above zero wherever a team-a job is waiting.

#### Import the full dashboard

The repository contains the complete dashboard as JSON, `dashboards/kueue-fleet.json`. It has four panels:

| Panel | Query | What it shows |
|---|---|---|
| Pending workloads per team | `sum by (cluster_queue) (kueue_pending_workloads)` | Queue depth: work waiting for admission |
| Admitted workloads per team | `sum by (cluster_queue) (kueue_admitted_active_workloads)` | Work running |
| GPUs in use vs quota | `kueue_cluster_queue_resource_usage` and `kueue_cluster_queue_nominal_quota` for `nvidia.com/gpu`, per queue | Usage as a solid line, quota as a dashed line; solid above dashed is borrowing |
| Preemptions (last 5 minutes) | `sum by (preempting_cluster_queue) (increase(kueue_preempted_workloads_total[5m]))` | Bars when a team's workload evicts another |

**In Grafana, in the browser:**

1. **Dashboards** → **New** → **Import**.
2. Click **Upload dashboard JSON file** and choose `kueue-fleet.json` from the `dashboards` folder of your clone (on Windows, `C:\projects\learning\gpu-fleet-labs\dashboards\kueue-fleet.json`).
3. Grafana asks for the **Prometheus** data source the dashboard should use; choose **Prometheus** and click **Import**.

The dashboard opens with a 30-minute window that refreshes every 10 seconds. You can delete `Kueue scratch` now, or keep it to experiment with.

#### Watch borrowing and reclaim

E3 and E4 depended on the jobs E2 left running, and E5 deleted those. This part rebuilds that state from nothing in three steps, so each change shows up on the dashboard on its own. Prometheus scrapes Kueue about every 30 seconds, so wait a minute after each step before you read the panels.

The steps use `job.sh` from B8, which gives every pod 8 GPUs. If you changed it during E6, the jobs below are rejected with `must be equal to nvidia.com/gpu limit of 8`. Check the file first, then clear both research teams.

**On the laptop (WSL2):**

```bash
cd ~/gpu-fleet-lab/kueue-sim
grep -n "nvidia.com/gpu" job.sh
kubectl delete jobs --all -n team-a
kubectl delete jobs --all -n team-b
```

The `grep` should print one line in which both the request and the limit read `nvidia.com/gpu: "8"`, alongside `cpu: "96"` and `memory: 1Ti`. If it shows anything else, recreate `job.sh` from B8. The two deletes leave both teams with nothing running; jobs in the `dev` namespace can stay.

**Step 1, the baseline.** team-a fills its own quota and team-b uses three quarters of its share.

**On the laptop (WSL2):**

```bash
./job.sh team-a research a-base 16      # 16 nodes = 128 GPUs, team-a's whole quota
./job.sh team-b research b-base 12      # 12 nodes = 96 GPUs of team-b's 128
```

After a minute, **GPUs in use vs quota** shows team-a's solid line on its dashed quota line at 128 and team-b's solid line at 96, below its quota. **Admitted** shows one workload per team.

**Step 2, borrowing (E3).** team-b's job finishes, and team-a asks for 12 more nodes.

**On the laptop (WSL2):**

```bash
kubectl -n team-b delete job b-base     # team-b goes idle
./job.sh team-a research a-extra 12     # 96 more GPUs for team-a
kubectl get clusterqueue team-a -o jsonpath='{.status.flavorsUsage}' | jq
```

The `jq` output shows `nvidia.com/gpu` with `total` 224 and `borrowed` 96. On the dashboard, team-a's solid line climbs to 224, above its dashed quota line, and team-b's falls to 0.

**Step 3, reclaim (E4).** team-b comes back with an 8-node job against its own quota.

**On the laptop (WSL2):**

```bash
./job.sh team-b research b-urgent 8     # 64 GPUs of team-b's guaranteed 128
kubectl -n team-a get workloads
```

Only 32 GPUs are free, so Kueue evicts `a-extra`, the team-a workload running on borrowed quota. In the `get workloads` output it is no longer admitted. On the dashboard, team-a's line drops back to 128, team-b's rises to 64, a bar appears in **Preemptions**, and **Pending workloads** shows team-a with one waiting job. `a-extra` stays pending because it needs 96 GPUs and the cohort has 64 free.

On a real fleet these four panels answer the question that decides whether a team needs Kueue at all: do the groups contend for GPUs, and how often? When you're finished, clear both teams with the two `kubectl delete jobs --all` commands above.

If a panel stays empty, check that Prometheus is scraping Kueue. In a second terminal, run `kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090` and open `http://localhost:9090` → **Status** → **Targets**: the Kueue target should be **UP**. Typing `kueue_` into the query box on Prometheus's main page lists the metric names your Kueue version exports. An empty **GPUs in use vs quota** panel with the others working means `enableClusterQueueResources` isn't in effect: repeat the first part of this exercise.

---

## Record

In `gpu-fleet-lab/notes/kueue.md`:

1. Draw the reference fleet as flavors and ClusterQueues. How many teams, what nominal quotas, what borrowing limits? What information would you need from the research leads to set them?
2. In E4, what did team-a lose? What checkpoint interval would you recommend for jobs that run on borrowed quota, and how would you tell researchers?
3. When is `blockAdmission: true` worth its cost?
4. Where does the `dev` queue's capacity come from, and should it be in the cohort?
5. Which dstack limitation from stage 2B does each exercise address?

## Going further

- **Fair sharing:** ClusterQueues can set `fairSharing` weights so borrowing is divided in proportion to weight rather than first come, first served. Read the Kueue fair-sharing docs and repeat E3 with three teams.
- **Topology-aware scheduling:** Kueue can place a job's pods within the same network block (on AWS, the same EFA spine). Add a topology label to the fake nodes and read the TAS docs; it matters at 32 nodes because NCCL performance depends on it.
- **Cohort hierarchy:** cohorts can nest (a `Cohort` object with a parent), which models "research division → team" quotas.

## Clean up

Delete the kind cluster once you have finished the exercises and the record.

**On the laptop (WSL2):**

```bash
kind delete cluster --name fleet-sim
```

This removes the Docker container that hosted the control plane, along with the fake nodes, Kueue and the monitoring stack.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Jobs complete within seconds | `pod-complete` stage still present | `kubectl delete stage pod-complete` |
| Workload admitted but pods Pending with "untolerated taint" | Flavor has no toleration | Check the ResourceFlavor `tolerations` block |
| Workload pending: "resource cpu unavailable in ClusterQueue" | Pod requests a resource the ClusterQueue doesn't cover | Add it to `coveredResources` and each flavor |
| `unknown field "cohort"` | Older field name | Kueue v1beta2 uses `cohortName` |
| Grafana shows no Kueue data | Prometheus not selecting Kueue's ServiceMonitor | Reinstall kube-prometheus-stack with `serviceMonitorSelectorNilUsesHelmValues=false` |

## References

- [Kueue — Installation](https://kueue.sigs.k8s.io/docs/getting-started/installation/)
- [Kueue — Administer cluster quotas](https://kueue.sigs.k8s.io/docs/tasks/manage/administer_cluster_quotas/)
- [Kueue — Preemption](https://kueue.sigs.k8s.io/docs/concepts/preemption/)
- [Kueue — WorkloadPriorityClass](https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/)
- [Kueue — All-or-nothing with ready pods](https://kueue.sigs.k8s.io/docs/tasks/manage/setup_wait_for_pods_ready/)
- [Kueue — v1beta2 API reference](https://kueue.sigs.k8s.io/docs/reference/kueue.v1beta2/)
- [kwok — Deploy in a cluster](https://kwok.sigs.k8s.io/docs/user/kwok-in-cluster/)
- [kwok — Stages](https://kwok.sigs.k8s.io/docs/user/stages-configuration/)
