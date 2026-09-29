# Prerequisites — every package source for the labs

[Labs index](README.md) · First lab: [Stage 2B — dstack](stage-2b-dstack.md)

One page listing every tool the labs use, where it comes from, and how to install it. The lab guides link here rather than repeating install steps, so each tool is installed one way only.

There are two machines, and every step says which one it's for:

| Machine | What it is | How you reach it |
|---|---|---|
| **GPU node** | The Ubuntu Server 24.04 box with the RTX 3070s (`gpu-node`, `192.168.1.50` in these guides) | Its console, or `ssh chris@192.168.1.50` from the laptop |
| **Laptop (WSL2)** | The Ubuntu 24.04 distribution inside WSL2 on your Windows laptop | Windows Terminal → Ubuntu tab |
| **AWS console (browser)** | Your AWS account's web console | Any browser on the laptop |

Section 1 is entirely on the GPU node; section 2 is entirely on the laptop; section 3 is the AWS account, set up in a browser and finished on the laptop. Code blocks contain no prompts, so paste them as they are.

Revised 2026-09-29: Docker now comes from **Docker's own apt repository** (`docker-ce`), replacing Ubuntu's `docker.io`. The two packages bring different containerd builds (`containerd.io` from Docker, `containerd` from Ubuntu), and mixing them produces the containerd errors you hit.

## At a glance

| Tool | Machine | Source | Installed in | Used by |
|---|---|---|---|---|
| NVIDIA driver | GPU node | Ubuntu repo (`ubuntu-drivers`) | [Stage 2B A2](stage-2b-dstack.md#a2-nvidia-driver) | All GPU labs |
| Docker Engine | GPU node | **Docker apt repo** | [§1.1](#11-docker-engine-from-dockers-repository) | Stage 2B (dstack SSH fleet) |
| NVIDIA Container Toolkit | GPU node | NVIDIA apt repo | [§1.2](#12-nvidia-container-toolkit) | Stages 2B, 2 |
| k3s | GPU node | k3s install script | [Stage 2 B1](stage-2-k8s-gpu-node.md#b1-k3s) | Stage 2 |
| Slurm, munge, python3-venv | GPU node | Ubuntu repo (universe) | [§1.3](#13-slurm-and-python-packages) | Slurm extension |
| Docker (for kind) | Laptop WSL2 | Docker Desktop, or Docker apt repo | [§2.1](#21-docker-for-kind) | Stage 1 |
| git, OpenSSH client, jq, unzip | Laptop WSL2 | Ubuntu repo | [§2.2](#22-base-packages) | All |
| uv, dstack | Laptop WSL2 | Astral installer, PyPI | [§2.3](#23-uv-and-dstack) | Stage 2B |
| kubectl | Laptop WSL2 | Kubernetes apt repo (pkgs.k8s.io) | [§2.4](#24-kubectl) | Stages 1, 2, 3 |
| Helm | Laptop WSL2 | Helm apt repo | [§2.5](#25-helm) | Stages 1, 2, 3 |
| kind | Laptop WSL2 | GitHub release binary | [§2.6](#26-kind) | Stage 1 |
| Terraform | Laptop WSL2 | HashiCorp apt repo | [§2.7](#27-terraform) | Stage 3 |
| AWS CLI v2 | Laptop WSL2 | AWS installer | [§2.8](#28-aws-cli-v2) | Stages 2B (D6), 3 |
| AWS account (Paid plan), budget, GPU quota | AWS console (browser) | aws.amazon.com | [§3.1–3.3](#3-aws-account) | Stages 2B (D6), 3 |
| AWS sign-in (Identity Center) | AWS console + laptop WSL2 | Your AWS account | [§3.4](#34-sign-in-for-the-cli-terraform-and-dstack) | Stages 2B (D6), 3 |

### Why third-party repositories

Ubuntu's own packages are frozen at the version current when 24.04 shipped, plus security fixes. Vendor repositories (Docker, NVIDIA, Kubernetes, Helm, HashiCorp) track current releases. Each repository is added the same way, and it's worth recognising the pattern:

1. Download the vendor's signing key into a keyring file (`/etc/apt/keyrings/` or `/usr/share/keyrings/`).
2. Add a source entry that names the repository URL and says it's signed by that key (`signed-by=`), so apt trusts that key for that repository only.
3. `apt update`, then install.

If a key is missing or wrong, `apt update` reports `NO_PUBKEY` or `The following signatures couldn't be verified` for that one repository.

---

## 1. GPU node (Ubuntu Server 24.04)

Assumes Ubuntu is installed with OpenSSH server, and the NVIDIA driver is in place ([stage 2B A2](stage-2b-dstack.md#a2-nvidia-driver)).

### 1.1 Docker Engine from Docker's repository

**Remove anything that conflicts.** This includes `docker.io` and Ubuntu's `containerd` and `runc`, which Docker's packages replace. `purge` also removes their configuration files, including any `/etc/containerd/config.toml` left by Ubuntu's containerd, which is a common source of containerd start-up errors. Images and volumes under `/var/lib/docker` are kept.

**On the GPU node:**

```bash
sudo systemctl stop docker docker.socket containerd 2>/dev/null
sudo apt purge -y $(dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc 2>/dev/null | cut -f1)
sudo apt autoremove -y
```

On a clean machine the `purge` line finds nothing and does nothing. If you had already installed the NVIDIA Container Toolkit against `docker.io`, rerun its last two commands in [§1.2](#12-nvidia-container-toolkit) after Docker is reinstalled, so Docker's configuration includes the `nvidia` runtime again.

**Add Docker's repository and install — on the GPU node:**

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

The `tee ... <<EOF` command is a *here-document*: the shell keeps reading lines (showing a `>` prompt) until it sees a line containing only `EOF`, then passes everything in between to `tee`. Paste the whole block from `sudo tee` through the closing `EOF` in one go. If you're left at a `>` prompt, press Ctrl+C and paste again.

The source file uses apt's newer deb822 format (`.sources`, one field per line); the NVIDIA and Kubernetes repositories below still use the one-line `.list` format. apt reads both.

**Let your user run Docker, and start it at boot — on the GPU node:**

```bash
sudo usermod -aG docker $USER
sudo systemctl enable --now docker containerd
newgrp docker          # or log out and back in
```

Membership of the `docker` group is equivalent to root on this machine. That's acceptable on a lab box you own.

**Check — on the GPU node:**

```bash
docker version                 # Server section shows Engine and containerd versions
apt-cache policy docker-ce     # installed version comes from download.docker.com
docker run --rm hello-world
systemctl status containerd --no-pager
```

**If containerd errors persist** after the reinstall, look at the log first: `journalctl -u containerd -b --no-pager | tail -50`. Leftover state from the Ubuntu package can be moved aside (Docker has no images yet, so nothing is lost):

**On the GPU node:**

```bash
sudo systemctl stop docker containerd
sudo mv /var/lib/containerd /var/lib/containerd.old
sudo systemctl start containerd docker
```

### 1.2 NVIDIA Container Toolkit

Install this *after* Docker, because its last step edits Docker's configuration.

**On the GPU node:**

```bash
sudo apt install -y --no-install-recommends ca-certificates curl gnupg2
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

`nvidia-ctk runtime configure` adds an `nvidia` runtime entry to `/etc/docker/daemon.json`. Docker then uses it whenever a container is started with `--gpus`.

**Check — on the GPU node:**

```bash
cat /etc/docker/daemon.json
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

k3s, installed in stage 2, detects the same toolkit and registers it with its own embedded containerd. Docker's `containerd.io` and k3s's containerd run side by side with separate sockets (`/run/containerd/containerd.sock` and `/run/k3s/containerd/containerd.sock`).

### 1.3 Slurm and Python packages

For the [Slurm extension](extension-slurm.md). Both come from Ubuntu's `universe` component, which Ubuntu Server enables by default (`grep -r universe /etc/apt/sources.list.d/ubuntu.sources` to confirm).

**On the GPU node:**

```bash
sudo apt install -y slurm-wlm munge python3-venv python3-pip
```

`python3-venv` is needed for `python3 -m venv`; Ubuntu ships Python without it.

---

## 2. Laptop (WSL2 Ubuntu 24.04)

Everything below runs inside WSL2. Check the distribution first: `lsb_release -a` should report 24.04 (noble).

### 2.1 Docker for kind

Stage 1 runs Kubernetes nodes as Docker containers. Pick **one** of these, not both in the same WSL distribution:

| Option | How | Notes |
|---|---|---|
| Docker Desktop (Windows) with WSL integration | Install Docker Desktop, then Settings → Resources → WSL integration → enable your Ubuntu distribution | Simplest. `docker` inside WSL talks to Docker Desktop |
| Docker Engine inside WSL2 | Run the commands from [§1.1](#11-docker-engine-from-dockers-repository) in the WSL2 terminal instead of on the GPU node | Needs systemd enabled in WSL (`/etc/wsl.conf`: `[boot]` `systemd=true`, then `wsl --shutdown` from PowerShell) |

Don't install `docker.io` in WSL either, and don't combine the two options: Docker Desktop and a WSL-installed engine both claim `/var/run/docker.sock`.

With Docker Desktop, your WSL user also needs to be in the `docker` group, which owns the socket; without it every `docker` command fails with `permission denied while trying to connect to the docker API`.

**On the laptop (WSL2):**

```bash
sudo usermod -aG docker $USER
```

Then close the Ubuntu terminal, run `wsl --shutdown` in PowerShell, and open Ubuntu again so the new group membership applies.

**Check, on the laptop (WSL2):** `docker run --rm hello-world`.

### 2.2 Base packages

**On the laptop (WSL2):**

```bash
sudo apt update
sudo apt install -y git openssh-client jq unzip curl ca-certificates gnupg
```

The dstack server requires git and OpenSSH; `jq` is used in stage 1; `unzip` by the AWS CLI installer.

### 2.3 uv and dstack

**On the laptop (WSL2):**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc                       # puts ~/.local/bin on PATH
uv tool install "dstack[all]" -U
dstack --version
```

`uv tool install` puts dstack in its own isolated environment, so it can't conflict with other Python packages. Upgrade later with the same command.

### 2.4 kubectl

From the Kubernetes project's apt repository. The URL names a minor version; kubectl works with clusters one minor version either side of it, so v1.36 covers k3s 1.36 (stage 2) and the EKS versions in stage 3.

**On the laptop (WSL2):**

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key \
    | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
sudo chmod 644 /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /' \
    | sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo chmod 644 /etc/apt/sources.list.d/kubernetes.list
sudo apt update && sudo apt install -y kubectl
kubectl version --client
```

To move to a later minor version, change `v1.36` in both lines and reinstall.

### 2.5 Helm

Helm 4 is current. From Helm's apt repository (hosted on Buildkite); the script checks the key's fingerprint before trusting it.

**On the laptop (WSL2):**

```bash
HELM_BUILDKITE_APT_KEY_ID="DDF78C3E6EBB2D2CC223C95C62BA89D07698DBC6"
sudo apt install -y curl gpg apt-transport-https
curl -fsSL https://packages.buildkite.com/helm-linux/helm-debian/gpgkey > /tmp/helm.gpg
[ "$(gpg --show-keys --with-colons /tmp/helm.gpg | awk -F: '$1 == "fpr" {print $10}' | head -n 1)" = "$HELM_BUILDKITE_APT_KEY_ID" ] \
    && echo "key OK" || echo "UNEXPECTED KEY - stop here"
gpg --dearmor < /tmp/helm.gpg | sudo tee /usr/share/keyrings/helm.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/helm.gpg] https://packages.buildkite.com/helm-linux/helm-debian/any/ any main" \
    | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list
sudo apt update && sudo apt install -y helm
helm version
```

Only continue past the fourth line if it prints `key OK`.

### 2.6 kind

A single binary from the project's releases; pin the version so the lab is repeatable.

**On the laptop (WSL2):**

```bash
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64
chmod +x ./kind && sudo mv ./kind /usr/local/bin/kind
kind version
```

### 2.7 Terraform

From HashiCorp's apt repository.

**On the laptop (WSL2):**

```bash
wget -O - https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(. /etc/os-release && echo "$VERSION_CODENAME") main" \
    | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install -y terraform
terraform version
```

If you already run Terraform on Windows for helix-core-on-azure, install it in WSL as well; the stage 3 commands assume a Linux shell.

### 2.8 AWS CLI v2

AWS distributes v2 as an installer rather than through apt (Ubuntu's `awscli` package is the old v1).

**On the laptop (WSL2):**

```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install
aws --version
```

## 3. AWS account

Needed for stage 2B exercise D6 and stage 3. Steps 3.1–3.3 happen in a web browser in the AWS console; step 3.4 finishes on the laptop (WSL2). Do them in order: each depends on the one before.

### 3.1 Create the account and choose the Paid plan

**In a browser:** sign up at [aws.amazon.com](https://aws.amazon.com/) with an email address you'll keep for this account. Sign-up asks you to choose an account plan.

| | Free plan | Paid plan |
|---|---|---|
| Sign-up credits | US$100, plus up to US$100 more for guided activities | Same |
| How long credits last | 6 months or until used, whichever comes first | Until used |
| At the end | **The account closes**; resources are deleted after 90 days unless you upgrade | Account stays open; you pay for usage beyond credits |
| Services | A subset; AWS excludes services that could quickly use up the credits | All services |

**Choose the Paid plan.** The Free plan excludes services that could use up the credits quickly, and GPU instances are the obvious candidates; AWS doesn't publish the exact list, so a Free-plan account risks hitting a wall at stage 3. Enabling Identity Center in §3.4 also creates an AWS Organization, which moves a Free-plan account to Paid anyway. The Paid plan still receives the same credits, and they're applied to your bills until used. At about US$2 an hour for the stage 3 cluster, US$200 of credits covers far more lab time than the plan needs.

**Secure the root user straight away:** in the console, open the account menu (top right) → **Security credentials** → **Assign MFA device**. Use the root user only for the account set-up below; day-to-day work uses the Identity Center user from §3.4.

### 3.2 Budget alert and activity credits

**In a browser, in the AWS console.** Both plans can earn an extra US$20 each for five guided activities, listed on the console home page under the credits widget. Complete them now: enabling Identity Center in §3.4 creates an AWS Organization, and after that the activities can no longer be earned.

| Activity | What to do | Also useful because |
|---|---|---|
| Set up a budget in AWS Budgets | **Billing and Cost Management → Budgets → Create budget → Monthly cost budget**, e.g. US$50, with an email alert at 80% | The safety net for every lab: you hear about a forgotten GPU node within a day |
| Launch an EC2 instance | Launch the smallest instance the activity suggests in us-east-2, then **terminate** it | First look at the EC2 console you'll use for GPU quotas |
| Create an RDS database | Follow the guided activity; **delete** the database afterwards | — |
| Build a Lambda web app | Follow the guided activity; delete the function afterwards | — |
| Use Amazon Bedrock | Submit one prompt in the Bedrock playground | — |

Credits show under **Billing and Cost Management → Credits**. Check that nothing from the activities is left running.

### 3.3 GPU quota in us-east-2

New accounts usually start with a quota of **0 vCPUs** for GPU instance families, so any GPU launch fails until you ask for more. Approval can take a few days and a new account may be asked to explain its use, so request it now.

**In a browser, in the AWS console,** with the region set to **US East (Ohio)**: **Service Quotas → AWS services → Amazon Elastic Compute Cloud (Amazon EC2) → Running On-Demand G and VT instances → Request increase at account level**, new value **16**. In the description, say it's for a personal Kubernetes GPU lab using two g6.xlarge instances.

The quota counts vCPUs, not instances: a g6.xlarge has 4 vCPUs, so 16 allows the two lab nodes with room for a larger instance type if you try one. The same request can be made from the CLI once §3.4 is done:

```bash
aws service-quotas request-service-quota-increase --region us-east-2 \
    --service-code ec2 --quota-code L-DB2E81BA --desired-value 16
```

### 3.4 Sign-in for the CLI, Terraform and dstack

The CLI, Terraform and dstack all need AWS credentials. `aws configure sso` doesn't use an IAM user: it signs you in as an **IAM Identity Center** user and hands the CLI short-lived credentials for a role. Nothing permanent is stored on the laptop, and the credentials expire after the session length you set. That user doesn't exist until you create it.

| Option | What you create | Credentials on the laptop | Use when |
|---|---|---|---|
| **IAM Identity Center** (recommended) | An Identity Center user, a permission set, and an assignment to your account | Temporary; renewed with `aws sso login` | Your own lab account; matches how most organizations give people AWS access |
| IAM user with access keys | An IAM user and an access key pair | Permanent keys in `~/.aws/credentials` | Quick start; keys must be protected and rotated |

**Identity Center set-up — in the AWS console (browser)**, signed in as root; done once, after §3.2:

1. Open **IAM Identity Center** directly at <https://us-east-2.console.aws.amazon.com/singlesignon/home?region=us-east-2> (or type *IAM Identity Center* in the console search bar and confirm the region menu shows **US East (Ohio)**), then choose **Enable**. IAM Identity Center is a separate service from **IAM**: the IAM console is global, shows "Global" in the region menu and manages IAM users and roles; Identity Center is regional and manages sign-in for people. This creates an AWS Organization with your account as its management account, which is how Identity Center manages access to accounts. It also moves a Free-plan account to the Paid plan and ends eligibility for the activity credits in §3.2, which is why those come first. The region you enabled it in is Identity Center's home region.
2. **Users → Add user**: username (for example `chris`), your email. Accept the invitation email and set a password and MFA.
3. **Permission sets → Create permission set → Predefined → AdministratorAccess**, with a session duration of 8 hours. The labs create IAM roles (stage 3 Pod Identity), which the narrower PowerUserAccess set can't do.
4. **AWS accounts** → select your account → **Assign users or groups** → `chris` → the AdministratorAccess permission set.
5. Identity Center **Dashboard** → **Settings summary** → copy the **AWS access portal URL**, which looks like `https://d-xxxxxxxxxx.awsapps.com/start`. It only exists once Identity Center is enabled. You can replace the `d-xxxxxxxxxx` part with a name of your choice under **Settings → Identity source → Customize**, which makes it easier to remember.

**On the laptop (WSL2):**

```bash
aws configure sso
```

Answer the prompts:

| Prompt | Answer |
|---|---|
| SSO session name | `lab` |
| SSO start URL | The access portal URL from step 5 |
| SSO region | `us-east-2` (Identity Center's home region from step 1) |
| SSO registration scopes | Press Enter for the default |

The CLI prints a URL and a code. Open the URL in your Windows browser, confirm the code, and sign in as `chris`. Back in WSL, pick the account and the AdministratorAccess role, then answer:

| Prompt | Answer |
|---|---|
| Default client region | `us-east-2` |
| CLI default output format | `json` |
| Profile name | `lab` |

Make it the default for this shell and check it:

```bash
echo 'export AWS_PROFILE=lab' >> ~/.bashrc && source ~/.bashrc
aws sts get-caller-identity      # shows an assumed-role ARN containing AWSReservedSSO_AdministratorAccess
```

When the session expires, commands fail with a token error; renew it with `aws sso login`. Terraform, dstack and `aws eks update-kubeconfig` read the same `AWS_PROFILE`, so one login covers all three.

**IAM user alternative, if you'd rather not create an Organization:** in the console (browser), **IAM → Users → Create user** (`lab-cli`), attach `AdministratorAccess`, then **Security credentials → Create access key → Command Line Interface**. On the laptop (WSL2), run `aws configure` and enter the key ID, secret, `us-east-2` and `json`. Delete the key when the labs are finished.

---

## Final check

| Machine | Command | Expect |
|---|---|---|
| GPU node | `nvidia-smi` | Driver 570 or later (CUDA 12.8 images need it); both GPUs once installed |
| GPU node | `docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi` | Same table, from a container |
| GPU node | `apt-cache policy docker-ce containerd.io \| grep -A1 Installed` | Versions from `download.docker.com` |
| GPU node | `dpkg -l docker.io containerd 2>/dev/null \| grep ^ii` | Nothing |
| Laptop | `docker run --rm hello-world` | Hello message |
| Laptop | `dstack --version; kubectl version --client; helm version; kind version; terraform version; aws --version` | A version from each |
| Laptop | `aws sts get-caller-identity` | Your account ID and an `AWSReservedSSO_AdministratorAccess` role |
| Laptop | `aws service-quotas get-service-quota --region us-east-2 --service-code ec2 --quota-code L-DB2E81BA --query Quota.Value` | `16.0` once the increase is approved |
| AWS console | Billing and Cost Management → Budgets | Your monthly budget with an alert |

## References

- [Docker — Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Docker — Linux post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/)
- [NVIDIA Container Toolkit — Installing](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [Kubernetes — Install kubectl on Linux](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)
- [Helm — Installing Helm](https://helm.sh/docs/intro/install/)
- [kind — Quick start](https://kind.sigs.k8s.io/docs/user/quick-start/)
- [HashiCorp — Install Terraform](https://developer.hashicorp.com/terraform/install)
- [AWS — Install the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- [AWS — Choosing a Free Tier plan](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html)
- [AWS — Free Tier FAQs](https://aws.amazon.com/free/free-tier-faqs/)
- [AWS — Configure the CLI with IAM Identity Center](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)
- [dstack — Installation](https://dstack.ai/docs/installation/)
