# API Gateway Unused-API Cleanup — Automation

End-to-end automation for finding and removing unused AWS API Gateway APIs.

## Repository Structure

```
automation/api-gateway-cleanup/
├── configs/
│   └── config.yaml            # Central configuration (thresholds, flags)
├── lambdas/
│   ├── lambda_scanner.py      # Lambda #1 — scans all APIs + CloudWatch metrics
│   ├── lambda_classifier.py   # Lambda #2 — classifies APIs into tiers
│   ├── lambda_notifier.py     # Lambda #3 — sends SES emails + SNS alerts
│   └── lambda_cleaner.py      # Lambda #4 — soft/hard deletes APIs
├── scripts/
│   ├── scan.py                # CLI — local scan, outputs CSV + JSON report
│   ├── cleanup.py             # CLI — applies soft/hard delete from report
│   └── archive.py             # CLI — exports OpenAPI specs to S3 before deletion
├── terraform/
│   ├── main.tf                # All AWS infrastructure (DynamoDB, S3, Lambda, SFN, EB)
│   └── terraform.tfvars       # Your environment values
└── requirements.txt
```

## Quick Start — Manual (CLI)

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Scan all APIs

```bash
python scripts/scan.py --output ./report
# Generates: ./report.csv and ./report.json
```

With a specific profile and region subset:

```bash
python scripts/scan.py \
  --profile myprofile \
  --regions us-east-1,eu-west-1 \
  --days 90 \
  --output ./report
```

### 3. Review the report

Open `report.csv` — APIs are sorted by severity (ORPHANED → DORMANT → LOW_TRAFFIC → ACTIVE).

### 4. Archive specs to S3 before any deletion

```bash
python scripts/archive.py \
  --report ./report.json \
  --bucket your-api-archive-bucket \
  --dry-run        # preview first
```

Remove `--dry-run` to actually upload.

### 5. Soft-delete (throttle to zero)

```bash
# Dry run first
python scripts/cleanup.py --report ./report.json --mode soft

# Apply
python scripts/cleanup.py --report ./report.json --mode soft --no-dry-run
```

### 6. Hard-delete (after 7-day monitoring window)

```bash
python scripts/cleanup.py --report ./report.json --mode hard --no-dry-run
```

---

## Quick Start — Automated (Terraform + Step Functions)

### Prerequisites

| Tool | Minimum version | Install |
|---|---|---|
| Terraform | 1.5 | https://developer.hashicorp.com/terraform/install |
| AWS CLI | v2 | https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html |

---

### Step 1 — Point Terraform at your AWS account

Terraform uses the AWS CLI credential chain. Pick **one** of the options below.

#### Option A — AWS CLI named profile (recommended for local use)

```bash
# Configure a named profile (interactive prompts)
aws configure --profile my-profile

# Enter when prompted:
#   AWS Access Key ID     → your IAM access key
#   AWS Secret Access Key → your IAM secret key
#   Default region        → us-east-1
#   Default output format → json
```

Then export the profile so Terraform picks it up automatically:

```bash
export AWS_PROFILE=my-profile
```

Or add it directly to `terraform/main.tf` provider block (not recommended for shared repos):

```hcl
provider "aws" {
  region  = var.aws_region
  profile = "my-profile"   # matches ~/.aws/credentials profile name
}
```

#### Option B — Environment variables (recommended for CI/CD)

```bash
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="your-secret"
export AWS_DEFAULT_REGION="us-east-1"
```

#### Option C — IAM Instance Profile / SSO (no keys needed)

If you are running on an EC2 instance, ECS task, or have AWS SSO configured, credentials
are resolved automatically — no extra configuration needed.

```bash
# Verify the identity Terraform will use
aws sts get-caller-identity
```

---

### Step 2 — Configure your values

Edit `terraform/terraform.tfvars` with your account-specific values:

```hcl
aws_region             = "us-east-1"                              # region to deploy into
environment            = "prod"

ses_sender_email       = "you@example.com"                        # must be verified in SES
sns_alert_email        = "you@example.com"                        # receives orphan alerts

s3_archive_bucket_name = "api-gateway-cleanup-archive-prod"       # must be globally unique
dynamodb_table_name    = "api-gateway-inventory"

lookback_days          = "90"
low_traffic_threshold  = "10"
dry_run                = "true"    # keep true until you have reviewed a full scan
soft_delete_window_days = "7"
notice_period_days      = "30"
```

> ⚠️ **SES:** By default AWS accounts are in SES sandbox mode.
> Verify `ses_sender_email` at **AWS Console → SES → Verified identities** before deploying.

---

### Step 3 — (Optional) Configure a remote state backend

By default Terraform stores state locally in `terraform.tfstate`.
For team use, store it in S3:

1. Create an S3 bucket and a DynamoDB lock table manually **once**:

```bash
# State bucket (pick a unique name)
aws s3api create-bucket \
  --bucket my-tf-state-bucket \
  --region us-east-1

aws s3api put-bucket-versioning \
  --bucket my-tf-state-bucket \
  --versioning-configuration Status=Enabled

# Lock table
aws dynamodb create-table \
  --table-name terraform-locks \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --region us-east-1
```

2. Add a backend block to `terraform/main.tf` (inside the `terraform {}` block):

```hcl
terraform {
  required_version = ">= 1.5"

  backend "s3" {
    bucket         = "my-tf-state-bucket"
    key            = "api-gateway-cleanup/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraform-locks"
    encrypt        = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
```

---

### Step 4 — Deploy

```bash
cd automation/api-gateway-cleanup/terraform

# Download the AWS provider plugin
terraform init

# Preview all resources that will be created
terraform plan -var-file="terraform.tfvars"

# Apply (type "yes" when prompted)
terraform apply -var-file="terraform.tfvars"
```

Terraform will output the key ARNs when done:

```
dynamodb_table_name = "api-gateway-inventory"
s3_archive_bucket   = "api-gateway-cleanup-archive-prod"
sns_topic_arn       = "arn:aws:sns:us-east-1:123456789012:api-gw-cleanup-orphan-alerts"
state_machine_arn   = "arn:aws:states:us-east-1:123456789012:stateMachine:api-gw-cleanup-pipeline"
scanner_lambda_arn  = "arn:aws:lambda:us-east-1:123456789012:function:api-gw-cleanup-scanner"
```

---

### Step 5 — Trigger manually (first run)

```bash
aws stepfunctions start-execution \
  --state-machine-arn <state_machine_arn from output above> \
  --input '{"triggered_by": "manual"}' \
  --region us-east-1
```

---

### Step 6 — Check the DynamoDB inventory

```bash
aws dynamodb scan \
  --table-name api-gateway-inventory \
  --filter-expression "tier IN (:d, :o)" \
  --expression-attribute-values '{":d":{"S":"DORMANT"},":o":{"S":"ORPHANED"}}' \
  --output table \
  --region us-east-1
```

---

### Step 7 — Flip to live mode when ready

```bash
# In terraform/terraform.tfvars
dry_run = "false"

terraform apply -var-file="terraform.tfvars"
```

---

### Tear down

```bash
terraform destroy -var-file="terraform.tfvars"
```

> ⚠️ The S3 bucket will fail to destroy if it contains objects. Empty it first:
> `aws s3 rm s3://api-gateway-cleanup-archive-prod --recursive`

---

## Pipeline Flow

```
EventBridge (every Monday 08:00 UTC)
           │
           ▼
   Step Functions Pipeline
           │
   ┌───────▼────────┐
   │  Lambda Scanner │  ← scans all REST/HTTP/WebSocket APIs across regions
   └───────┬────────┘
           │
   ┌───────▼────────────┐
   │ Lambda Classifier   │  ← tags each API: ACTIVE / LOW_TRAFFIC / DORMANT / ORPHANED
   └───────┬────────────┘
           │
   ┌───────▼────────┐
   │ Lambda Notifier │  ← sends SES email to owner, escalates after 14 days
   └───────┬────────┘
           │
      Wait (7 days)
           │
   ┌───────▼────────┐
   │ Lambda Cleaner  │  ← SOFT: throttles stage to 0, archives spec to S3
   │   (soft mode)   │
   └───────┬────────┘
           │
      Wait (7 days)
           │
   ┌───────▼────────┐
   │ Lambda Cleaner  │  ← HARD: deletes API, marks deleted_at in DynamoDB
   │   (hard mode)   │
   └────────────────┘
```

---

## Configuration Reference

| Key | Default | Description |
|---|---|---|
| `lookback_days` | `90` | Days of CloudWatch history to analyse |
| `low_traffic_threshold` | `10` | req/day below which API is LOW_TRAFFIC |
| `dormant_days` | `30` | Zero-traffic days before DORMANT |
| `notice_period_days` | `30` | Days to wait for owner response |
| `escalation_days` | `14` | Days before auto-escalation |
| `soft_delete_window_days` | `7` | Days between soft and hard delete |
| `dry_run` | `true` | Set `false` to enable real deletions |

---

## CI/CD — Jenkins Provisioning Pipeline

A `Jenkinsfile` is provided at `automation/api-gateway-cleanup/Jenkinsfile`.
It provisions (or tears down) all AWS services using **AWS CLI only** — no Terraform required on the Jenkins agent.

### Jenkins Prerequisites

| Requirement | Details |
|---|---|
| AWS CLI v2 | Installed on the Jenkins agent |
| Python 3 + `zip` | For packaging Lambda functions |
| IAM permissions | Agent must have an IAM role / instance profile (see below) |
| Jenkins plugins | *Pipeline*, *AnsiColor*, *Timestamper* |

### IAM permissions required on the Jenkins agent role

The Jenkins agent's IAM role needs at minimum:

```json
{
  "Effect": "Allow",
  "Action": [
    "iam:CreateRole", "iam:GetRole", "iam:PutRolePolicy",
    "iam:ListRolePolicies", "iam:DeleteRolePolicy", "iam:DeleteRole",
    "lambda:CreateFunction", "lambda:UpdateFunctionCode",
    "lambda:UpdateFunctionConfiguration", "lambda:GetFunction",
    "lambda:GetFunctionConfiguration", "lambda:DeleteFunction",
    "dynamodb:CreateTable", "dynamodb:DescribeTable",
    "dynamodb:UpdateContinuousBackups", "dynamodb:DeleteTable",
    "s3:CreateBucket", "s3:HeadBucket", "s3:PutBucketVersioning",
    "s3:PutBucketEncryption", "s3:PutPublicAccessBlock",
    "s3:PutLifecycleConfiguration",
    "sns:CreateTopic", "sns:Subscribe", "sns:GetTopicAttributes",
    "sns:DeleteTopic", "sns:ListTopics",
    "states:CreateStateMachine", "states:UpdateStateMachine",
    "states:DescribeStateMachine", "states:ListStateMachines",
    "states:DeleteStateMachine",
    "scheduler:CreateSchedule", "scheduler:UpdateSchedule",
    "scheduler:GetSchedule", "scheduler:DeleteSchedule",
    "sts:GetCallerIdentity"
  ],
  "Resource": "*"
}
```

### Pipeline Parameters

| Parameter | Default | Description |
|---|---|---|
| `ACTION` | `provision` | `provision` — create/update all services · `deprovision` — delete all |
| `AWS_REGION` | `us-east-1` | Target AWS region |
| `ENVIRONMENT` | `prod` | Environment tag (`prod` / `staging` / `dev`) |
| `SES_SENDER_EMAIL` | — | Verified SES sender address |
| `SNS_ALERT_EMAIL` | — | Email to subscribe to the SNS alert topic |
| `DRY_RUN` | `true` | Lambda env flag — set `false` for real deletions |
| `LOOKBACK_DAYS` | `90` | CloudWatch history window |
| `LOW_TRAFFIC_THRESHOLD` | `10` | req/day threshold |
| `SOFT_DELETE_WINDOW_DAYS` | `7` | Days between soft/hard delete |
| `NOTICE_PERIOD_DAYS` | `30` | Owner notice period |

### Provision — pipeline stages

```
Checkout
  └─► Verify AWS Access          (sts get-caller-identity)
        └─► DynamoDB Table        (create + PITR)
              └─► S3 Bucket       (create + versioning + encryption + lifecycle)
                    └─► SNS Topic  (create + email subscription)
                          └─► IAM Lambda Role
                                └─► Lambda Functions x4  (zip + create/update)
                                      └─► IAM Step Functions Role
                                            └─► Step Functions State Machine
                                                  └─► IAM Scheduler Role
                                                        └─► EventBridge Scheduler
                                                              └─► Smoke Check
```

### Usage

1. Create a **Pipeline** job in Jenkins pointing to this repo.
2. Ensure the Jenkins agent EC2 instance has the IAM role above attached.
3. Run with **ACTION = provision** — all AWS services are created in order.
4. Re-running provision is **idempotent** — existing resources are updated, not duplicated.
5. Run with **ACTION = deprovision** to tear everything down (prompts for confirmation).

---

## Safety Checklist

Before flipping `dry_run = false`:

- [ ] SES sender email is verified in AWS SES
- [ ] SNS subscription is confirmed
- [ ] S3 archive bucket exists and is accessible
- [ ] At least one full dry-run scan has been reviewed
- [ ] Team leads have acknowledged the process
- [ ] Terraform state is backed up
