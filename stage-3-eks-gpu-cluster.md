# Stage 3 — A small GPU cluster on EKS

[Labs index](README.md) · Previous: [Extension — Slurm](extension-slurm.md) · Next: [Stage 4 — Write-ups](stage-4-writeups.md)

## Why this lab

Stage 2 covered the GPU software stack on hardware you own. This lab covers what only AWS adds: EKS with GPU node groups built from AWS's NVIDIA AMI, the node monitoring agent and automatic repair, training across two machines over the network, identity-based access to S3, optional FSx for Lustre, and cost by tag. It runs in **us-east-2**, the reference fleet's region, using the same Kueue and GPU Operator configuration as stage 2.

By the end you will have:

- a Terraform-built VPC and EKS cluster with a system node group and a GPU node group (2 × g6.xlarge);
- Kueue admitting a two-node PyTorch job as one unit and NCCL running across machines;
- read GPU health conditions from the node monitoring agent;
- reached S3 from a pod with no keys, through EKS Pod Identity;
- found the session's cost by tag, and torn it all down.

**Budget:** roughly $2 per hour while running (EKS control plane, NAT gateway, one small system node, two g6.xlarge), plus about $0.25 per hour if you create the optional FSx file system. Destroy at the end of every session.

**Prerequisites:** the AWS account, approved GPU quota and Identity Center sign-in from [prerequisites §3](00-prerequisites.md#3-aws-account), and Terraform, the AWS CLI, kubectl and Helm in WSL2 ([prerequisites §2](00-prerequisites.md#2-laptop-wsl2-ubuntu-2404)). Confirm your credentials with `aws sts get-caller-identity` before the first `terraform apply`.

## Where things run

Everything in this lab runs **on the laptop, in WSL2**: Terraform, the AWS CLI, `kubectl` and `helm`, with the Terraform and YAML files kept in `~/gpu-fleet-lab/eks`. The home GPU node isn't used; the GPUs are in AWS.

## Concepts

### How the home lab maps onto EKS

| Home (stage 2) | EKS (this lab) | Notes |
|---|---|---|
| Ubuntu + NVIDIA driver + Container Toolkit, installed by you | **EKS-optimized AL2023 NVIDIA AMI** | Ships driver, toolkit, EFA kernel module, fabric manager. Does not ship the device plugin |
| k3s | EKS control plane (managed) + managed node groups | You never see the control-plane machines |
| GPU Operator with driver and toolkit off | Same | Adds device plugin, GFD, DCGM exporter |
| Nothing watches GPU health | **Node monitoring agent** add-on + **node auto repair** | Detects Xid, ECC, NVLink, thermal faults; reboots or replaces the node |
| `local-path` volume | S3 (and optionally FSx for Lustre) | Shared storage across nodes |
| Kubeconfig file with a token | IAM identity mapped to cluster access | `enable_cluster_creator_admin_permissions` grants you admin |

### Training across two machines

On one machine NCCL moved data through host memory. Between machines it needs a network transport:

| Transport | When | NCCL log shows |
|---|---|---|
| TCP sockets | Any instance; used here | `NET/Socket` |
| EFA via the `aws-ofi-nccl` plugin | EFA-capable instances in a cluster placement group (p4d, p5, p6, larger g5/g6 sizes) | `NET/OFI` and `efa` |

g6.xlarge has no EFA, so this lab runs over TCP. The mechanics (rendezvous, ranks, all-reduce) are identical; the bandwidth is not. On a p5 fleet each node has 3,200 Gbps of EFA and the jobs depend on it.

Rendezvous: every rank must find rank 0 (`MASTER_ADDR`). In Kubernetes an **Indexed Job** gives each pod a stable name (`ddp-0`, `ddp-1`) and a **headless Service** gives those names DNS records, so rank 1 can reach `ddp-0.ddp`.

### Pod Identity

Pods get AWS permissions without access keys. You create an IAM role that trusts the EKS Pod Identity service, then associate it with a Kubernetes service account in a namespace. Any pod running as that service account receives temporary credentials for the role. This is how training jobs on a production fleet reach datasets and checkpoints in S3 within an organization's security controls.

---

## Build

### B1. Terraform layout

```
gpu-fleet-lab/eks/
├── versions.tf      providers and remote state
├── network.tf       VPC
├── eks.tf           cluster, node groups, add-ons
├── storage.tf       S3 bucket, Pod Identity role
└── fsx.tf           optional FSx for Lustre
```

`versions.tf` pins the Terraform and AWS provider versions and configures the backend. Keep state in S3 as you did for helix-core-on-azure (there it was an Azure storage account; the idea is the same).

**File on the laptop (WSL2):** `~/gpu-fleet-lab/eks/versions.tf`

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {
    bucket = "<your-tf-state-bucket>"
    key    = "gpu-fleet-lab/eks.tfstate"
    region = "us-east-2"
  }
}

provider "aws" {
  region = "us-east-2"
  default_tags {
    tags = {
      project    = "gpu-fleet-lab"
      team       = "lab"
      experiment = var.experiment   # change per session to split costs
    }
  }
}

variable "experiment" { default = "session-1" }
```

`default_tags` stamps every resource. That's the basis of cost attribution, and on a production fleet the equivalent tags (team, project, experiment) would be enforced rather than optional.

### B2. Network

`network.tf` builds the VPC across two Availability Zones in us-east-2, with private subnets for the nodes and public subnets for the NAT gateway.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/eks/network.tf`

```hcl
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = "gpu-lab"
  cidr = "10.40.0.0/16"
  azs  = ["us-east-2a", "us-east-2b"]

  private_subnets = ["10.40.0.0/20", "10.40.16.0/20"]
  public_subnets  = ["10.40.128.0/24", "10.40.129.0/24"]

  enable_nat_gateway = true
  single_nat_gateway = true   # one NAT for the lab; production uses one per AZ
}
```

Nodes live in private subnets and reach the internet (image pulls, pip) through the NAT gateway. One NAT saves money and gives up AZ redundancy, a trade-off worth stating explicitly in the design.

### B3. Cluster and node groups

Check which Kubernetes versions EKS offers (`aws eks describe-cluster-versions --region us-east-2`) and use the newest standard-support one. `eks.tf` defines the cluster, its add-ons and two managed node groups.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/eks/eks.tf`

```hcl
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = "gpu-lab"
  kubernetes_version = "1.35"          # set from the command above

  endpoint_public_access                   = true
  enable_cluster_creator_admin_permissions = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  addons = {
    coredns                   = {}
    kube-proxy                = {}
    vpc-cni                   = { before_compute = true }
    eks-pod-identity-agent    = { before_compute = true }
    eks-node-monitoring-agent = {}
  }

  eks_managed_node_groups = {
    system = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = ["t3.large"]
      min_size       = 1
      max_size       = 1
      desired_size   = 1
    }

    gpu = {
      ami_type       = "AL2023_x86_64_NVIDIA"
      instance_types = ["g6.xlarge"]     # 1 x NVIDIA L4, 24 GB
      min_size       = 0
      max_size       = 2
      desired_size   = 2
      subnet_ids     = [module.vpc.private_subnets[0]]   # one AZ, as a training cluster would be

      labels = { "gpu-type" = "l4" }
      taints = {
        gpu = { key = "nvidia.com/gpu", value = "true", effect = "NO_SCHEDULE" }
      }

      node_repair_config = { enabled = true }
    }
  }
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --region us-east-2 --name ${module.eks.cluster_name}"
}
```

| Setting | Why |
|---|---|
| `AL2023_x86_64_NVIDIA` | AWS's GPU AMI: driver and toolkit preinstalled, as on the host in stage 2 |
| Taint on the GPU group | Keeps non-GPU pods (CoreDNS, Prometheus) off expensive nodes; GPU pods tolerate it |
| GPU group in one subnet (one AZ) | Multi-node training traffic shouldn't cross AZs: it adds latency and costs money per GB. Capacity Blocks for p5 are placed in a single AZ for the same reason |
| `eks-node-monitoring-agent` | Watches kernel logs and DCGM for GPU faults, sets node conditions |
| `node_repair_config` | Lets EKS act on those conditions: reboot or replace the node |
| `desired_size = 2`, `min_size = 0` | Scale to zero between sessions without destroying the cluster, if you prefer that to a full destroy |

Create the cluster, point kubectl at it, and list the nodes with their GPU label and instance type.

**On the laptop (WSL2):**

```bash
terraform init && terraform apply
$(terraform output -raw configure_kubectl)
kubectl get nodes -L gpu-type,node.kubernetes.io/instance-type
```

Cluster creation takes 10–15 minutes; that's the control plane being built. When it finishes, the node list shows one t3.large and two g6.xlarge nodes, with `l4` in the GPU-type column for the g6 pair.

### B4. GPU Operator, monitoring and Kueue

Install the same three Helm charts as stage 2 B2–B5, pointed at this cluster.

**On the laptop (WSL2):**

```bash
helm install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace --version=v26.7.1 \
    --set driver.enabled=false --set toolkit.enabled=false \
    --set dcgmExporter.serviceMonitor.enabled=true --wait
helm install kueue oci://registry.k8s.io/kueue/charts/kueue --version=0.19.6 \
    -n kueue-system --create-namespace --wait
```

The GPU Operator's pods must tolerate the GPU taint; the chart's defaults include a toleration for `nvidia.com/gpu`. Check that `nvidia-device-plugin-daemonset` pods are running on both GPU nodes and that each node advertises `nvidia.com/gpu: 1`.

The Kueue objects are the same as stage 2's, with a different flavor. Copy `~/gpu-fleet-lab/k8s/kueue-home.yaml` to `~/gpu-fleet-lab/eks/kueue-eks.yaml`, replace its ResourceFlavor with the one below, and change the flavor name in both ClusterQueues from `rtx3070` to `l4`. The new flavor selects the L4 nodes and tolerates their taint.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/eks/kueue-eks.yaml` (ResourceFlavor section)

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata: {name: l4}
spec:
  nodeLabels: {gpu-type: l4}
  tolerations:
  - {key: nvidia.com/gpu, operator: Equal, value: "true", effect: NoSchedule}
```

Kueue adds these node labels and tolerations to the pods of every workload it admits under this flavor, so job specs don't need their own.

Set team-a's GPU `nominalQuota` to 2 so it can run the two-node job, and size its CPU and memory quotas to what the two g6.xlarge nodes can actually offer (4 vCPU and 16 GiB each, less system daemons). `kubectl describe node` shows each node's allocatable figures. Then apply the file.

**On the laptop (WSL2):**

```bash
cd ~/gpu-fleet-lab/eks
kubectl apply -f kueue-eks.yaml
kubectl get clusterqueues,localqueues -A
```

Both ClusterQueues and both LocalQueues should be listed against the EKS cluster. If a ClusterQueue is inactive, check that its flavor name matches the ResourceFlavor.

---

## Exercises

### E1. A two-node training job

This manifest defines a headless Service and an Indexed Job that runs one training rank on each GPU node. It runs in namespace `team-a`; create the `lab-code` ConfigMap there first, as in stage 2 B6.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/eks/ddp.yaml`

```yaml
apiVersion: v1
kind: Service
metadata: {name: ddp}
spec:
  clusterIP: None                  # headless: DNS records per pod, no load balancing
  publishNotReadyAddresses: true   # resolve ddp-0 before it reports Ready
  selector: {job-name: ddp}
  ports: [{name: rdzv, port: 29500}]
---
apiVersion: batch/v1
kind: Job
metadata:
  name: ddp
  labels: {kueue.x-k8s.io/queue-name: research}
spec:
  completionMode: Indexed          # pods named ddp-0, ddp-1; index in JOB_COMPLETION_INDEX
  parallelism: 2
  completions: 2
  backoffLimit: 0
  template:
    spec:
      subdomain: ddp               # with the Service, gives ddp-0.ddp a DNS name
      restartPolicy: Never
      containers:
      - name: trainer
        image: pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
        command: ["bash", "-c"]
        args:
        - >
          torchrun --nnodes=2 --nproc-per-node=1
          --node-rank=$JOB_COMPLETION_INDEX
          --master-addr=ddp-0.ddp --master-port=29500
          /code/train_ddp.py --steps 2000 --batch-size 256
        env:
        - {name: NCCL_DEBUG, value: INFO}
        resources:
          requests: {cpu: "2", memory: 8Gi, nvidia.com/gpu: "1"}   # g6.xlarge: 4 vCPU, 16 GiB, minus system daemons
          limits: {nvidia.com/gpu: "1"}
        volumeMounts:
        - {name: code, mountPath: /code}
        - {name: dshm, mountPath: /dev/shm}
      volumes:
      - {name: code, configMap: {name: lab-code}}
      - {name: dshm, emptyDir: {medium: Memory, sizeLimit: 4Gi}}
```

The Service name, the pod `subdomain` and `--master-addr` all use `ddp`, which is how rank 1 finds rank 0. The queue label submits the Job to Kueue's `research` queue.

Submit the job, confirm the two pods landed on different GPU nodes, and follow rank 0's log.

**On the laptop (WSL2):**

```bash
kubectl -n team-a apply -f ddp.yaml
kubectl -n team-a get pods -o wide        # one pod per GPU node
kubectl -n team-a logs ddp-0-<suffix> -f
```

**What to notice:**

- Kueue admitted both pods together: the gang admission from stage 1, now with real machines.
- In the log, NCCL reports `NET/Socket` and the network interface it chose. Compare with stage 2's `SHM`.
- Scaling efficiency against a 1-node run. Over TCP between two L4s it will be well below the single-machine figure. Run `preflight.py` with the same shape (`--nnodes=2`) to get the bus bandwidth and put it beside stage 2's number.

Then read the gap to production: on p5.48xlarge, NCCL would log the OFI plugin and EFA provider, and each node would have 32 EFA interfaces. Look up what the aws-ofi-nccl plugin does and why cluster placement groups are required; they explain most multi-node performance problems on p5.

### E2. GPU health conditions

Read the node conditions the monitoring agent sets, then search cluster events for GPU-related entries.

**On the laptop (WSL2):**

```bash
kubectl get nodes -o custom-columns='NAME:.metadata.name,ACCEL:.status.conditions[?(@.type=="AcceleratedHardwareReady")].status,REASON:.status.conditions[?(@.type=="AcceleratedHardwareReady")].reason'
kubectl describe node <gpu-node> | sed -n '/Conditions:/,/Addresses:/p'
kubectl get events -A | grep -i -E 'xid|nvidia|accelerated'
```

**What to notice:** the agent adds node conditions (`AcceleratedHardwareReady` among them) alongside Kubernetes' usual ones. When a condition turns unhealthy, auto repair acts after a grace period: Xid codes that a reset can clear trigger a reboot; hardware faults such as Xid 79 (GPU fell off the bus) trigger replacement. You can't safely fake an Xid on rented hardware, so read the [node monitoring agent](https://docs.aws.amazon.com/eks/latest/userguide/node-health-nma.html) and [node repair](https://docs.aws.amazon.com/eks/latest/userguide/node-repair.html) pages and map each action onto the failure runbook you'll write in stage 4.

Question to answer: with a Capacity Block, "replace the node" means getting a healthy instance *inside the reservation*. What happens to a running 32-node job when one node is replaced, and what should the scheduler do?

### E3. S3 through Pod Identity

`storage.tf` creates an S3 bucket, an IAM role that only the EKS Pod Identity service can assume, and an association that ties the role to the `trainer` service account in `team-a`.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/eks/storage.tf`

```hcl
resource "aws_s3_bucket" "lab" {
  bucket_prefix = "gpu-lab-"
  force_destroy = true
}

data "aws_iam_policy_document" "pod_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "trainer" {
  name_prefix        = "gpu-lab-trainer-"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
}

data "aws_iam_policy_document" "bucket_rw" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.lab.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.lab.arn}/*"]
  }
}

resource "aws_iam_role_policy" "trainer" {
  role   = aws_iam_role.trainer.id
  policy = data.aws_iam_policy_document.bucket_rw.json
}

resource "aws_eks_pod_identity_association" "trainer" {
  cluster_name    = module.eks.cluster_name
  namespace       = "team-a"
  service_account = "trainer"
  role_arn        = aws_iam_role.trainer.arn
}

output "bucket" { value = aws_s3_bucket.lab.bucket }
```

The role's policy allows listing the bucket and reading, writing and deleting its objects. `force_destroy` lets `terraform destroy` remove the bucket while it still holds objects.

Apply the new resources, create the service account, then list the bucket from two pods: one as `trainer`, one as the default service account.

**On the laptop (WSL2):**

```bash
terraform apply
kubectl -n team-a create serviceaccount trainer
kubectl -n team-a run s3test --rm -it --restart=Never --image=amazon/aws-cli \
    --overrides='{"spec":{"serviceAccountName":"trainer"}}' -- s3 ls s3://$(terraform output -raw bucket)
kubectl -n team-a run s3test2 --rm -it --restart=Never --image=amazon/aws-cli -- s3 ls
```

**What to notice:** the first pod lists the bucket with no credentials anywhere in its spec. The second, running as the default service account, is refused. Permissions follow the workload's identity, scoped to one namespace and one service account. Describe the first pod while it runs: EKS injected `AWS_CONTAINER_CREDENTIALS_FULL_URI` and a token volume.

### E4. Optional — FSx for Lustre as shared checkpoint storage

FSx for Lustre is what gives every node in a training job the same fast file system. Create it for one session only.

**File on the laptop (WSL2):** `~/gpu-fleet-lab/eks/fsx.tf`

```hcl
resource "aws_security_group" "fsx" {
  name_prefix = "gpu-lab-fsx-"
  vpc_id      = module.vpc.vpc_id
  ingress {
    from_port   = 988
    to_port     = 988
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }
  ingress {
    from_port   = 1018
    to_port     = 1023
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_fsx_lustre_file_system" "lab" {
  storage_capacity   = 1200                       # smallest size, in GiB
  deployment_type    = "SCRATCH_2"                # no replication; fine for scratch and checkpoints you also export
  subnet_ids         = [module.vpc.private_subnets[0]]   # same AZ as the GPU nodes
  security_group_ids = [aws_security_group.fsx.id]
  import_path        = "s3://${aws_s3_bucket.lab.bucket}"
  export_path        = "s3://${aws_s3_bucket.lab.bucket}/export"
}

output "fsx_id"        { value = aws_fsx_lustre_file_system.lab.id }
output "fsx_dns"       { value = aws_fsx_lustre_file_system.lab.dns_name }
output "fsx_mountname" { value = aws_fsx_lustre_file_system.lab.mount_name }
```

The security group opens the Lustre ports (988 and 1018–1023) to the VPC. The file system sits in the GPU nodes' subnet, imports the bucket's contents and exports to its `export/` prefix. The three outputs are the values the PersistentVolume below needs.

Add the `aws-fsx-csi-driver` add-on to the `addons` map, apply, then create a static PersistentVolume and claim that point at the file system (the [FSx CSI driver static provisioning example](https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/master/examples/kubernetes/static_provisioning) shows the fields: `volumeHandle` = file system ID, `volumeAttributes.dnsname` and `mountname`). Mount it at `/ckpt` in the `ddp` job and add `--ckpt-dir /ckpt/ddp`.

**What to notice:** kill `ddp-1` mid-run; the Job fails (`backoffLimit: 0`), you resubmit, and both ranks resume from the checkpoint rank 0 wrote to the shared file system. Without shared storage, a restarted job on different nodes couldn't find its checkpoint. Also look at the S3 import: objects in the bucket appear as files in `/ckpt` without being copied first; Lustre loads them on first read. That's how large datasets reach a training fleet.

Destroy FSx as soon as you're done: `terraform destroy -target=aws_fsx_lustre_file_system.lab`.

### E5. What the session cost

In the Billing console, activate `project`, `team` and `experiment` as **cost allocation tags** (one-time; tags only appear in reports for usage after activation, and data can take up to a day). Next day, in Cost Explorer, group by tag `experiment`.

**What to notice:** the split between GPU instances, NAT gateway, EKS control plane and data transfer. The GPU line dominates. At the reference fleet's scale, cost per experiment is GPU-hours × the effective rate of the block or reservation, and the tags have to be on the *jobs* (Kubernetes labels exported by Kubecost/OpenCost or the scheduler's own accounting), since 32 shared nodes serve many experiments.

---

## Record

In `gpu-fleet-lab/notes/eks.md`:

1. Bus bandwidth: stage 2 (one machine, SHM) vs stage 3 (two machines, TCP). What would you expect on p5 with EFA?
2. What does node auto repair do for each Xid class, and what does the job scheduler need to do when a node disappears mid-job?
3. How would you give ten research teams S3 access without sharing credentials? Sketch the IAM and Pod Identity layout.
4. What did the session cost per GPU-hour, all-in, versus the GPU instance's list price? Where did the difference come from?
5. Which parts of this Terraform would you reuse for the reference fleet, and what would change (instance types, placement groups, Capacity Block reservations, EFA)?

## Clean up

Remove the Helm releases, then destroy the Terraform resources.

**On the laptop (WSL2):**

```bash
helm uninstall gpu-operator -n gpu-operator; helm uninstall kps -n monitoring; helm uninstall kueue -n kueue-system
terraform destroy
```

Uninstall Helm releases first: anything that created AWS resources outside Terraform's knowledge (load balancers, volumes) can block VPC deletion. Then check the EC2 console for leftover instances, volumes or load balancers.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| GPU node group stuck creating | GPU vCPU quota still 0, or g6 capacity short in that AZ | Service Quotas in us-east-2; try `us-east-2b` or `g5.xlarge` |
| Nodes Ready but no `nvidia.com/gpu` | Device plugin not running on the tainted nodes | `kubectl -n gpu-operator get pods -o wide`; check tolerations |
| `ddp-1` can't resolve `ddp-0.ddp` | Service missing, name mismatch, or `subdomain` not set | The Service name, `subdomain` and `--master-addr` must agree |
| NCCL hangs at start across nodes | Security group blocks pod-to-pod traffic on high ports | The EKS module's node security group allows node-to-node traffic by default; check any custom rules |
| `AccessDenied` from S3 | Pod not using the `trainer` service account, or wrong namespace | The association is per namespace + service account |
| `terraform destroy` hangs on the VPC | Load balancer or ENI left by a Kubernetes object | Uninstall Helm releases first; delete stray ENIs in the console |

## References

- [terraform-aws-modules/eks](https://github.com/terraform-aws-modules/terraform-aws-eks)
- [terraform-aws-modules/vpc](https://github.com/terraform-aws-modules/terraform-aws-vpc)
- [EKS — Accelerated AMIs](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-optimized-ami.html)
- [EKS — Node monitoring agent](https://docs.aws.amazon.com/eks/latest/userguide/node-health-nma.html)
- [EKS — Node auto repair](https://docs.aws.amazon.com/eks/latest/userguide/node-repair.html)
- [EKS — Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html)
- [FSx for Lustre CSI driver](https://github.com/kubernetes-sigs/aws-fsx-csi-driver)
- [aws-ofi-nccl (NCCL over EFA)](https://github.com/aws/aws-ofi-nccl)
- [Kubernetes — Indexed Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/#completion-mode)
