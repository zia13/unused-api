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

### 1. Configure

Edit `terraform/terraform.tfvars`:

```hcl
aws_region       = "us-east-1"
ses_sender_email = "mdziaur.rahman@corebridgefinancial.com"
sns_alert_email  = "mdziaur.rahman@corebridgefinancial.com"
dry_run          = "true"   # flip to "false" when ready for live deletions
```

### 2. Deploy

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

### 3. Trigger manually (first run)

```bash
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN from terraform output> \
  --input '{"triggered_by": "manual"}'
```

### 4. Check the DynamoDB inventory

```bash
aws dynamodb scan \
  --table-name api-gateway-inventory \
  --filter-expression "tier IN (:d, :o)" \
  --expression-attribute-values '{":d":{"S":"DORMANT"},":o":{"S":"ORPHANED"}}' \
  --output table
```

### 5. Flip to live mode when ready

```bash
# In terraform.tfvars
dry_run = "false"
terraform apply
```

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

## CI/CD — Jenkins Pipeline

A `Jenkinsfile` is provided at `automation/api-gateway-cleanup/Jenkinsfile`.

### Jenkins Prerequisites

| Requirement | Details |
|---|---|
| Terraform | ≥ 1.5 installed on the Jenkins agent |
| AWS CLI | v2 installed on the Jenkins agent |
| Python 3 | For lambda zip packaging |
| Jenkins plugins | *Pipeline*, *AnsiColor*, *Credentials Binding*, *Timestamper* |

### Jenkins Credentials (configure in *Manage Jenkins → Credentials*)

| Credential ID | Kind | Description |
|---|---|---|
| `aws-access-key-id` | AWS Credentials / Secret text | IAM access key ID |
| `aws-secret-access-key` | Secret text | IAM secret access key |
| `ses-sender-email` | Secret text | Verified SES sender address |
| `sns-alert-email` | Secret text | SNS alert recipient address |

### Pipeline Parameters

| Parameter | Default | Description |
|---|---|---|
| `ACTION` | `plan` | `plan` / `apply` / `destroy` |
| `ENVIRONMENT` | `prod` | `prod` / `staging` / `dev` |
| `AWS_REGION` | `us-east-1` | Target AWS region |
| `DRY_RUN` | `true` | Enable real deletions after deploy |
| `AUTO_APPROVE` | `false` | Skip manual approval gate |
| `LOOKBACK_DAYS` | `90` | CloudWatch history window |
| `LOW_TRAFFIC_THRESHOLD` | `10` | req/day threshold |
| `SOFT_DELETE_WINDOW_DAYS` | `7` | Days between soft/hard delete |
| `NOTICE_PERIOD_DAYS` | `30` | Owner notice period |

### Pipeline Stages

```
Checkout
  └─► Validate Prerequisites   (terraform, aws cli, python3 versions + creds check)
        └─► Generate tfvars     (writes terraform.tfvars from credentials + params)
              └─► Terraform Init
                    └─► Terraform Validate
                          └─► Terraform Plan    (archives tfplan.txt as artifact)
                                └─► Approval Gate  (manual confirm for apply/destroy)
                                      ├─► Terraform Apply   → Smoke Test
                                      └─► Terraform Destroy
```

### Usage

1. Create a *Pipeline* job in Jenkins pointing to this repo.
2. Add the four credentials listed above.
3. Run with **ACTION = plan** first to preview changes.
4. Run with **ACTION = apply** and review the approval prompt before confirming.

---

## Safety Checklist

Before flipping `dry_run = false`:

- [ ] SES sender email is verified in AWS SES
- [ ] SNS subscription is confirmed
- [ ] S3 archive bucket exists and is accessible
- [ ] At least one full dry-run scan has been reviewed
- [ ] Team leads have acknowledged the process
- [ ] Terraform state is backed up
