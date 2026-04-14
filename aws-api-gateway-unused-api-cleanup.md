# AWS API Gateway — Unused API Cleanup: Design & Process Document

**Version:** 2.0  
**Date:** April 13, 2026  
**Status:** Active  
**Audience:** Platform Engineering, Cloud Infrastructure, DevOps Teams

---

## Table of Contents

1. [Overview & Goals](#1-overview--goals)
2. [Definition of "Unused"](#2-definition-of-unused)
3. [Discovery Process](#3-discovery-process)
4. [Analysis Methodology](#4-analysis-methodology)
5. [Classification Framework](#5-classification-framework)
6. [Stakeholder Communication & Approval](#6-stakeholder-communication--approval)
7. [Safe Cleanup Process](#7-safe-cleanup-process)
8. [Rollback & Recovery Plan](#8-rollback--recovery-plan)
9. [Automation Approach](#9-automation-approach)
10. [Governance & Ongoing Monitoring](#10-governance--ongoing-monitoring)
11. [Appendix: Reference Commands](#11-appendix-reference-commands)

---

## 1. Overview & Goals

### 1.1 Business Drivers

Unused APIs in AWS API Gateway accumulate silently over time and create several organisational risks:

| Driver | Description |
|---|---|
| **Cost** | Each API Gateway stage with custom domains, WAF associations, or data transfer incurs charges even with zero invocations. |
| **Security** | Orphaned APIs with lingering IAM policies, API keys, or Lambda integrations expand the attack surface unnecessarily. |
| **Operational overhead** | Teams spend time investigating alerts, logs, and quota limits for APIs that serve no traffic. |
| **Compliance** | Unmanaged endpoints may violate internal security policies or regulatory requirements (e.g. PCI-DSS, SOC2). |

### 1.2 Scope

This process applies to all three API Gateway types across **all AWS regions and all accounts** in the organisation:

- **REST APIs** (v1)
- **HTTP APIs** (v2)
- **WebSocket APIs** (v2)

### 1.3 Success Metrics

- Number of APIs identified as unused
- Number of APIs safely decommissioned
- Estimated monthly cost saving (USD)
- Reduction in API Gateway quota usage per region
- Zero production incidents caused by accidental deletion

---

## 2. Definition of "Unused"

An API is considered **unused** if it meets one or more of the following criteria over a configurable look-back window (default: **90 days**).

### 2.1 Primary Signals

| Signal | CloudWatch Metric | Threshold |
|---|---|---|
| Zero invocations | `Count` | `SUM == 0` over look-back window |
| No successful responses | `5XXError`, `4XXError` | All requests erroring (no healthy traffic) |
| No integration latency recorded | `IntegrationLatency` | `SampleCount == 0` |

### 2.2 Secondary Signals

- **No deployment**: API has no active stage or deployment
- **No usage plan / API key**: REST API has no usage plan attached (suggests it was never integrated into a client)
- **No custom domain**: API has never been given a friendly DNS name
- **No CloudWatch log group**: Access logging was never configured
- **No WAF association**: API was never protected, suggesting it may be a forgotten test API

### 2.3 Look-back Window

The default look-back window is **90 days**. Teams can override this per API via the `last-reviewed` tag. APIs tagged `lifecycle: protected` are excluded from automated cleanup.

---

## 3. Discovery Process

### 3.1 Prerequisites

- AWS CLI v2 installed and configured
- Appropriate IAM permissions (see §3.3)
- AWS Organizations access (for multi-account discovery)

### 3.2 Step-by-Step Discovery

#### Step 1 — List all active AWS accounts

```bash
aws organizations list-accounts \
  --query 'Accounts[?Status==`ACTIVE`].[Id,Name]' \
  --output table
```

#### Step 2 — List all enabled regions

```bash
aws account list-regions \
  --region-opt-status-contains ENABLED \
  --query 'Regions[].RegionName' \
  --output text
```

#### Step 3 — Enumerate REST APIs (v1) in a region

```bash
aws apigateway get-rest-apis \
  --region <REGION> \
  --query 'items[*].{ID:id,Name:name,Created:createdDate}' \
  --output table
```

#### Step 4 — Enumerate HTTP & WebSocket APIs (v2) in a region

```bash
aws apigatewayv2 get-apis \
  --region <REGION> \
  --query 'Items[*].{ID:ApiId,Name:Name,Protocol:ProtocolType,Created:CreatedDate}' \
  --output table
```

#### Step 5 — Export inventory to JSON

```bash
# REST APIs
aws apigateway get-rest-apis --region <REGION> \
  --output json > inventory-rest-<REGION>.json

# HTTP + WebSocket APIs
aws apigatewayv2 get-apis --region <REGION> \
  --output json > inventory-v2-<REGION>.json
```

#### Step 6 — Assume cross-account role for multi-account scanning

```bash
CREDS=$(aws sts assume-role \
  --role-arn arn:aws:iam::<ACCOUNT_ID>:role/APIGatewayReadOnlyRole \
  --role-session-name cleanup-scan)

export AWS_ACCESS_KEY_ID=$(echo $CREDS | jq -r '.Credentials.AccessKeyId')
export AWS_SECRET_ACCESS_KEY=$(echo $CREDS | jq -r '.Credentials.SecretAccessKey')
export AWS_SESSION_TOKEN=$(echo $CREDS | jq -r '.Credentials.SessionToken')
```

### 3.3 Required IAM Permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "apigateway:GET",
        "apigatewayv2:GET",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "logs:DescribeLogGroups",
        "logs:StartQuery",
        "logs:GetQueryResults",
        "tag:GetResources",
        "config:SelectAggregateResourceConfig",
        "account:ListRegions",
        "organizations:ListAccounts"
      ],
      "Resource": "*"
    }
  ]
}
```

### 3.4 AWS Config Aggregator Query

For organisations using AWS Config, run the following advanced query to retrieve all API Gateway resources in one call:

```sql
SELECT
  resourceId,
  resourceName,
  resourceType,
  awsRegion,
  accountId,
  configuration,
  tags
WHERE
  resourceType IN (
    'AWS::ApiGateway::RestApi',
    'AWS::ApiGatewayV2::Api'
  )
```

```bash
aws configservice select-aggregate-resource-config \
  --configuration-aggregator-name OrgAggregator \
  --expression "<SQL above>" \
  --output json > config-inventory.json
```

---

## 4. Analysis Methodology

### 4.1 Pull Invocation Count per API (CloudWatch)

```bash
aws cloudwatch get-metric-statistics \
  --namespace "AWS/ApiGateway" \
  --metric-name "Count" \
  --dimensions Name=ApiName,Value=<API_NAME> \
  --start-time $(date -u -v-90d '+%Y-%m-%dT%H:%M:%SZ') \
  --end-time $(date -u '+%Y-%m-%dT%H:%M:%SZ') \
  --period 7776000 \
  --statistics Sum \
  --output json
```

> A `Sum` of `0` or a `Datapoints` array that is empty means no traffic in the period.

### 4.2 Query Access Logs via CloudWatch Logs Insights

If access logging is enabled on a stage, run the following Logs Insights query to confirm zero real requests:

```
fields @timestamp, @message
| filter @logStream like /access/
| stats count() as requestCount by bin(1d)
| sort @timestamp desc
| limit 20
```

```bash
aws logs start-query \
  --log-group-name "API-Gateway-Execution-Logs_<API_ID>/<STAGE>" \
  --start-time $(date -v-90d +%s) \
  --end-time $(date +%s) \
  --query-string 'stats count() as hits | filter @message like /HTTP/' \
  --output json
```

### 4.3 Check for Stages and Deployments

```bash
# List stages for a REST API
aws apigateway get-stages \
  --rest-api-id <API_ID> \
  --region <REGION>

# List deployments
aws apigateway get-deployments \
  --rest-api-id <API_ID> \
  --region <REGION>
```

An API with **no stages** or **no deployments** is an immediate candidate for deletion.

### 4.4 Check Usage Plans

```bash
aws apigateway get-usage-plans --region <REGION> \
  --query 'items[*].{Name:name,APIs:apiStages}' \
  --output table
```

APIs not referenced by any usage plan have never been formally integrated into a client workflow.

### 4.5 Cost Attribution via Cost Explorer

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-01-01,End=2026-04-13 \
  --granularity MONTHLY \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon API Gateway"]}}' \
  --group-by '[{"Type":"DIMENSION","Key":"USAGE_TYPE"}]' \
  --metrics "UnblendedCost" \
  --output table
```

---

## 5. Classification Framework

Each API discovered in §3 is assigned one of four tiers after analysis in §4.

### Tier Definitions

| Tier | Criteria | Recommended Action | Timeline |
|---|---|---|---|
| **Active** | Traffic in the last 30 days | No action | — |
| **Low-Traffic** | < 10 requests/day averaged over 90 days | Tag for review; notify owner | Review within 60 days |
| **Dormant** | Zero invocations for 30–90 days, but has active deployment/stage | Notify owner; begin deprecation | 30-day notice, then cleanup |
| **Orphaned** | No stage, no deployment, no usage plan, or no traffic ever | Immediate cleanup candidate | 7-day notice, then delete |

### Classification Decision Tree

```
Does the API have any invocations in the last 90 days?
├── YES
│   └── Avg > 10 req/day? → ACTIVE
│       Avg < 10 req/day? → LOW-TRAFFIC
└── NO
    ├── Has an active stage/deployment? → DORMANT
    └── No stage / no deployment?      → ORPHANED
```

---

## 6. Stakeholder Communication & Approval

### 6.1 Owner Identification

1. Check the `owner` and `team` resource tags on the API.
2. Fall back to the IAM principal that created the API (CloudTrail lookup).
3. Fall back to the AWS account owner if no other signal is available.

```bash
# Check tags on a REST API
aws apigateway get-tags \
  --resource-arn arn:aws:apigateway:<REGION>::/restapis/<API_ID>

# Check tags on an HTTP/WebSocket API
aws apigatewayv2 get-tags \
  --resource-arn arn:aws:apigateway:<REGION>::/apis/<API_ID>
```

### 6.2 Notification Template

**Subject:** `[ACTION REQUIRED] Unused AWS API Gateway API scheduled for deletion — <API_NAME> (<API_ID>)`

```
Hi <OWNER_NAME>,

Our automated API hygiene scan has identified the following API Gateway endpoint
as unused based on 90 days of CloudWatch metrics:

  API Name:    <API_NAME>
  API ID:      <API_ID>
  Region:      <REGION>
  Account:     <ACCOUNT_ID> (<ACCOUNT_NAME>)
  Last seen:   <LAST_TRAFFIC_DATE or "Never">
  Tier:        <DORMANT | ORPHANED>

Scheduled deletion date: <DATE + 30 days>

Actions you can take before deletion:
  1. Reply to confirm this API can be deleted.
  2. Reply to request a 30-day extension (one extension allowed).
  3. Tag the API with `lifecycle: protected` to permanently exclude it.

If no response is received by <DATE + 14 days>, deletion will proceed automatically.

Questions? Contact: platform-engineering@yourcompany.com
```

### 6.3 Approval Workflow

```
Scan identifies Dormant/Orphaned API
          │
          ▼
   Notify owner via email + Jira ticket
          │
     ┌────┴────┐
     │         │
  Response   No response
  received    in 14 days
     │              │
     ▼              ▼
Owner approves?  Escalate to team lead
   YES → proceed    │
   NO  → keep API   ▼
                Auto-approve after
                30-day total window
```

### 6.4 Jira Ticket Schema

| Field | Value |
|---|---|
| **Issue Type** | Task |
| **Summary** | `[API Cleanup] Delete unused API: <API_NAME> (<API_ID>)` |
| **Labels** | `api-cleanup`, `aws`, `api-gateway` |
| **Components** | Platform Engineering |
| **Priority** | Low |
| **Due Date** | Scheduled deletion date |
| **Custom fields** | `api_id`, `region`, `account_id`, `tier`, `owner_email` |

---

## 7. Safe Cleanup Process

### 7.1 Pre-Deletion Checklist

Before deleting any API, confirm all of the following:

- [ ] API is classified as Dormant or Orphaned (§5)
- [ ] Owner has been notified and notice period has elapsed (§6)
- [ ] OpenAPI spec has been exported and archived to S3 (§8.1)
- [ ] Jira ticket approval is recorded
- [ ] No active CloudWatch alarms reference this API
- [ ] No Lambda event-source mappings reference this API
- [ ] Custom domain (if any) has been detached

### 7.2 Phase 1 — Soft Deprecation (Day 0)

**Disable all usage plans / throttle to zero** (REST APIs):

```bash
aws apigateway update-stage \
  --rest-api-id <API_ID> \
  --stage-name <STAGE> \
  --patch-operations '[
    {"op":"replace","path":"/defaultRouteSettings/throttlingBurstLimit","value":"0"},
    {"op":"replace","path":"/defaultRouteSettings/throttlingRateLimit","value":"0"}
  ]'
```

**Remove custom domain mapping** (if any):

```bash
# REST API
aws apigateway delete-base-path-mapping \
  --domain-name <DOMAIN> \
  --base-path <BASE_PATH>

# HTTP/WebSocket API
aws apigatewayv2 delete-api-mapping \
  --domain-name <DOMAIN> \
  --api-mapping-id <MAPPING_ID>
```

**Monitor for 7 days.** If any `Count` metric > 0 appears, halt and re-investigate.

### 7.3 Phase 2 — Hard Delete (Day 7+)

**Delete REST API (v1):**

```bash
aws apigateway delete-rest-api \
  --rest-api-id <API_ID> \
  --region <REGION>
```

**Delete HTTP or WebSocket API (v2):**

```bash
aws apigatewayv2 delete-api \
  --api-id <API_ID> \
  --region <REGION>
```

### 7.4 Dry-Run Mode

Before running deletions in bulk, always perform a dry run using `--dry-run` (where supported) or by echoing commands:

```bash
DRY_RUN=true

delete_api() {
  local api_id=$1
  local region=$2
  if [ "$DRY_RUN" = "true" ]; then
    echo "[DRY RUN] Would delete REST API: $api_id in $region"
  else
    aws apigateway delete-rest-api --rest-api-id "$api_id" --region "$region"
  fi
}
```

---

## 8. Rollback & Recovery Plan

### 8.1 Pre-Deletion Archival to S3

Export and store the full API definition before any deletion:

```bash
# Export OpenAPI 3.0 spec (REST API)
aws apigateway get-export \
  --rest-api-id <API_ID> \
  --stage-name <STAGE> \
  --export-type oas30 \
  --accepts application/json \
  /tmp/<API_ID>-oas30.json

# Upload to S3 archive bucket
aws s3 cp /tmp/<API_ID>-oas30.json \
  s3://your-api-archive-bucket/api-gateway/<ACCOUNT_ID>/<REGION>/<API_ID>/<DATE>-oas30.json
```

Also export Swagger format for compatibility:

```bash
aws apigateway get-export \
  --rest-api-id <API_ID> \
  --stage-name <STAGE> \
  --export-type swagger \
  --accepts application/json \
  /tmp/<API_ID>-swagger.json
```

### 8.2 Re-Import Procedure

If an API needs to be restored after deletion:

```bash
# Re-import REST API from OpenAPI spec
aws apigateway import-rest-api \
  --fail-on-warnings \
  --body fileb:///tmp/<API_ID>-oas30.json

# Re-deploy to a stage
aws apigateway create-deployment \
  --rest-api-id <NEW_API_ID> \
  --stage-name <STAGE>
```

### 8.3 RTO / RPO Targets

| Scenario | RTO | RPO |
|---|---|---|
| Accidental deletion of non-critical API | 2 hours | 0 (spec archived pre-deletion) |
| Accidental deletion of critical API | 30 minutes | 0 (spec archived pre-deletion) |
| Full region failure | N/A (redeploy from IaC) | 0 |

### 8.4 Terraform / CloudFormation State

If the API was originally provisioned via IaC:

- **Terraform**: Run `terraform state rm aws_api_gateway_rest_api.<RESOURCE>` before deletion to avoid state drift. Archive the `.tf` files to S3.
- **CloudFormation**: Note the Stack name and template S3 location before deleting the resource outside of CloudFormation.

---

## 9. Automation Approach

> **All automation code lives in:** [`automation/api-gateway-cleanup/`](./automation/api-gateway-cleanup/)  
> See the [automation README](./automation/api-gateway-cleanup/README.md) for full setup and usage instructions.

---

### 9.1 Repository Structure

```
automation/api-gateway-cleanup/
├── configs/
│   └── config.yaml            # Central config — thresholds, flags, bucket names
├── lambdas/
│   ├── lambda_scanner.py      # Lambda #1 — scans all APIs + CloudWatch metrics
│   ├── lambda_classifier.py   # Lambda #2 — classifies APIs into tiers
│   ├── lambda_notifier.py     # Lambda #3 — sends SES email + SNS alerts
│   └── lambda_cleaner.py      # Lambda #4 — soft/hard deletes APIs + archives specs
├── scripts/
│   ├── scan.py                # CLI — local scan → CSV + JSON report
│   ├── cleanup.py             # CLI — applies soft/hard delete from report
│   └── archive.py             # CLI — exports OpenAPI specs to S3
├── terraform/
│   ├── main.tf                # Full IaC: DynamoDB, S3, IAM, Lambda, Step Functions, EventBridge
│   └── terraform.tfvars       # Environment-specific values
└── requirements.txt           # boto3, pyyaml
```

---

### 9.2 Pipeline Architecture

```
EventBridge Scheduler (every Monday 08:00 UTC)
                  │
                  ▼
       Step Functions State Machine
                  │
    ┌─────────────▼─────────────┐
    │     Lambda: Scanner        │  Scans all REST/HTTP/WebSocket APIs
    │     lambda_scanner.py      │  across all enabled regions via boto3.
    │                            │  Pulls 90-day CloudWatch Count metric.
    │                            │  Upserts records → DynamoDB inventory.
    └─────────────┬─────────────┘
                  │
    ┌─────────────▼─────────────┐
    │    Lambda: Classifier      │  Reads DynamoDB inventory.
    │    lambda_classifier.py    │  Applies tier rules (§5).
    │                            │  Writes tier + classified_at back.
    └─────────────┬─────────────┘
                  │
    ┌─────────────▼─────────────┐
    │     Lambda: Notifier       │  Queries DORMANT/ORPHANED records.
    │     lambda_notifier.py     │  Sends SES email to tag:owner.
    │                            │  Publishes to SNS on escalation.
    │                            │  Sets notified_at + deletion_scheduled_date.
    └─────────────┬─────────────┘
                  │
             Wait 7 days
             (SFN Wait state)
                  │
    ┌─────────────▼─────────────┐
    │  Lambda: Cleaner (soft)    │  Exports + archives OpenAPI spec → S3.
    │  lambda_cleaner.py         │  Throttles all stages to 0 req/s.
    │  CLEANER_MODE=soft         │  Sets soft_deleted_at in DynamoDB.
    └─────────────┬─────────────┘
                  │
             Wait 7 days
             (SFN Wait state)
                  │
    ┌─────────────▼─────────────┐
    │  Lambda: Cleaner (hard)    │  Calls delete-rest-api / delete-api.
    │  lambda_cleaner.py         │  Sets deleted_at in DynamoDB.
    │  CLEANER_MODE=hard         │  Full audit trail preserved.
    └───────────────────────────┘
```

---

### 9.3 DynamoDB Inventory Table Schema

**Table name:** `api-gateway-inventory`  
**Partition key:** `api_id` (String) | **Sort key:** `region` (String)  
**GSI:** `tier-index` on `tier` (for efficient tier-based queries)

| Attribute | Type | Description |
|---|---|---|
| `api_id` | String | API Gateway API ID |
| `region` | String | AWS region |
| `account_id` | String | AWS account ID |
| `api_name` | String | Human-readable name |
| `protocol` | String | `REST`, `HTTP`, `WEBSOCKET` |
| `created_date` | String | ISO8601 creation date |
| `last_invocation` | String | ISO8601 of last recorded traffic, or `never` |
| `invocation_count_90d` | Number | Sum of `Count` metric over 90 days |
| `has_stages` | Boolean | Whether API has an active stage |
| `tier` | String | `ACTIVE`, `LOW_TRAFFIC`, `DORMANT`, `ORPHANED` |
| `classified_at` | String | ISO8601 of last classification run |
| `owner_email` | String | Resolved from `owner` tag or fallback |
| `notified_at` | String | ISO8601 of last notification sent |
| `approved_for_deletion` | Boolean | Owner approval flag |
| `deletion_scheduled_date` | String | ISO8601 scheduled deletion date |
| `soft_deleted_at` | String | ISO8601 of throttle-to-zero |
| `archived_s3_key` | String | S3 key of the exported OpenAPI spec |
| `deleted_at` | String | ISO8601 of actual hard deletion |
| `scanned_at` | String | ISO8601 of last scan |
| `tags` | Map | Raw AWS resource tags |

---

### 9.4 Configuration Reference

All settings are controlled via environment variables on the Lambda functions (sourced from `configs/config.yaml` and `terraform/terraform.tfvars`):

| Variable | Default | Description |
|---|---|---|
| `DYNAMODB_TABLE` | `api-gateway-inventory` | DynamoDB table name |
| `S3_ARCHIVE_BUCKET` | `your-api-archive-bucket` | S3 bucket for OpenAPI spec archives |
| `SNS_TOPIC_ARN` | — | SNS topic for orphan alerts |
| `SES_SENDER_EMAIL` | — | Verified SES sender |
| `LOOKBACK_DAYS` | `90` | CloudWatch metric look-back window |
| `LOW_TRAFFIC_THRESHOLD` | `10` | req/day threshold for LOW_TRAFFIC tier |
| `DORMANT_DAYS` | `30` | Zero-traffic days before DORMANT |
| `DRY_RUN` | `true` | **Must be `false` to allow real deletions** |
| `SOFT_DELETE_WINDOW_DAYS` | `7` | Days between soft and hard delete |
| `NOTICE_PERIOD_DAYS` | `30` | Days owners have to respond |
| `ESCALATION_DAYS` | `14` | Days before auto-escalation |
| `CLEANER_MODE` | `soft` | `soft` or `hard` — set per Lambda invocation |

---

### 9.5 Quick Start — Manual (CLI)

```bash
# 1. Install dependencies
pip install -r automation/api-gateway-cleanup/requirements.txt

# 2. Scan all APIs (dry run, no AWS writes)
python automation/api-gateway-cleanup/scripts/scan.py \
  --profile myprofile \
  --days 90 \
  --output ./report

# 3. Review report.csv (sorted by severity)

# 4. Archive specs to S3 before deletion
python automation/api-gateway-cleanup/scripts/archive.py \
  --report ./report.json \
  --bucket your-api-archive-bucket \
  --dry-run

# 5. Soft delete (throttle to zero) — preview first
python automation/api-gateway-cleanup/scripts/cleanup.py \
  --report ./report.json --mode soft

# 6. Apply soft delete
python automation/api-gateway-cleanup/scripts/cleanup.py \
  --report ./report.json --mode soft --no-dry-run

# 7. Hard delete after monitoring window (7+ days later)
python automation/api-gateway-cleanup/scripts/cleanup.py \
  --report ./report.json --mode hard --no-dry-run
```

---

### 9.6 Quick Start — Automated (Terraform)

```bash
# 1. Configure
cd automation/api-gateway-cleanup/terraform
cp terraform.tfvars terraform.tfvars.local  # edit your values

# 2. Deploy all infrastructure
terraform init
terraform plan
terraform apply

# 3. Trigger first run manually
aws stepfunctions start-execution \
  --state-machine-arn $(terraform output -raw state_machine_arn) \
  --input '{"triggered_by": "manual"}'

# 4. Watch the execution
aws stepfunctions describe-execution \
  --execution-arn <ARN from above>

# 5. Flip to live mode when satisfied
# Edit terraform.tfvars: dry_run = "false"
terraform apply
```

---

### 9.7 EventBridge Schedule

The Step Functions pipeline runs automatically every **Monday at 08:00 UTC** via an EventBridge Scheduler rule (provisioned by Terraform). To trigger on-demand:

```bash
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --input '{"triggered_by": "manual", "tier_filter": "ORPHANED"}'
```

---

### 9.8 SNS Alert for New Orphaned APIs

```bash
# Create the alert topic (handled by Terraform, or manually)
aws sns create-topic --name api-gateway-orphan-alerts

aws sns subscribe \
  --topic-arn arn:aws:sns:<REGION>:<ACCOUNT_ID>:api-gateway-orphan-alerts \
  --protocol email \
  --notification-endpoint platform-engineering@yourcompany.com
```

---

## 10. Governance & Ongoing Monitoring

### 10.1 Mandatory Tagging Policy

All new API Gateway APIs **must** include the following tags at creation time:

| Tag Key | Example Value | Required |
|---|---|---|
| `owner` | `jane.doe@yourcompany.com` | ✅ Yes |
| `team` | `payments-platform` | ✅ Yes |
| `environment` | `production` | ✅ Yes |
| `project` | `checkout-v2` | ✅ Yes |
| `lifecycle` | `active` or `protected` | ✅ Yes |
| `last-reviewed` | `2026-04-13` | ✅ Yes |
| `cost-center` | `CC-1042` | ✅ Yes |

Enforce this via an AWS Config rule:

```json
{
  "ConfigRuleName": "api-gateway-required-tags",
  "Source": {
    "Owner": "AWS",
    "SourceIdentifier": "REQUIRED_TAGS"
  },
  "Scope": {
    "ComplianceResourceTypes": [
      "AWS::ApiGateway::RestApi",
      "AWS::ApiGatewayV2::Api"
    ]
  },
  "InputParameters": "{\"tag1Key\":\"owner\",\"tag2Key\":\"team\",\"tag3Key\":\"environment\",\"tag4Key\":\"lifecycle\"}"
}
```

### 10.2 CloudWatch Alarm: Traffic Drop to Zero

Create an alarm that fires when an API's `Count` metric drops to zero for 7 consecutive days:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "api-zero-traffic-<API_NAME>" \
  --alarm-description "API has received zero traffic for 7 days" \
  --namespace "AWS/ApiGateway" \
  --metric-name "Count" \
  --dimensions Name=ApiName,Value=<API_NAME> \
  --statistic Sum \
  --period 86400 \
  --evaluation-periods 7 \
  --threshold 1 \
  --comparison-operator LessThanThreshold \
  --treat-missing-data breaching \
  --alarm-actions arn:aws:sns:<REGION>:<ACCOUNT_ID>:api-gateway-orphan-alerts
```

### 10.3 Quarterly Review Cadence

| Activity | Frequency | Owner |
|---|---|---|
| Full inventory scan | Weekly (automated) | Platform Engineering |
| Review LOW_TRAFFIC APIs | Monthly | API Owners |
| Review DORMANT / ORPHANED queue | Bi-weekly | Platform Engineering |
| Bulk deletion execution | Quarterly | Platform Engineering + Security |
| Policy and threshold review | Quarterly | Engineering Manager |
| Cost saving report | Quarterly | FinOps / Cloud Costs team |

### 10.4 Service Catalog Integration

Register a **Service Catalog product** for API Gateway API creation that:

1. Enforces the mandatory tag policy (§10.1)
2. Automatically creates a DynamoDB record in the inventory table
3. Creates a CloudWatch zero-traffic alarm (§10.2)
4. Assigns a default `lifecycle: active` tag
5. Sends a welcome notification to the owner with a link to the cleanup policy

### 10.5 Prevention: SCP to Block Untagged API Creation

Optionally enforce tagging at the AWS Organizations level using a Service Control Policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyAPIGatewayWithoutOwnerTag",
      "Effect": "Deny",
      "Action": [
        "apigateway:POST"
      ],
      "Resource": "*",
      "Condition": {
        "Null": {
          "aws:RequestTag/owner": "true"
        }
      }
    }
  ]
}
```

---

## 11. Appendix: Reference Commands

### Quick Health Check for a Single API

```bash
API_ID="abc123def"
REGION="us-east-1"

echo "=== Stages ==="
aws apigateway get-stages --rest-api-id $API_ID --region $REGION

echo "=== Tags ==="
aws apigateway get-tags \
  --resource-arn "arn:aws:apigateway:${REGION}::restapis/${API_ID}"

echo "=== Last 90-day invocation count ==="
aws cloudwatch get-metric-statistics \
  --namespace "AWS/ApiGateway" \
  --metric-name "Count" \
  --dimensions Name=ApiId,Value=$API_ID \
  --start-time $(date -u -v-90d '+%Y-%m-%dT%H:%M:%SZ') \
  --end-time $(date -u '+%Y-%m-%dT%H:%M:%SZ') \
  --period 7776000 \
  --statistics Sum \
  --region $REGION
```

### Bulk List All APIs Across All Regions

```bash
for region in $(aws account list-regions \
  --region-opt-status-contains ENABLED \
  --query 'Regions[].RegionName' --output text); do
  echo "=== Region: $region ==="
  aws apigateway get-rest-apis --region $region \
    --query 'items[*].{ID:id,Name:name}' --output table 2>/dev/null
  aws apigatewayv2 get-apis --region $region \
    --query 'Items[*].{ID:ApiId,Name:Name,Type:ProtocolType}' --output table 2>/dev/null
done
```

### Find APIs With No Stages (Orphaned)

```bash
for api_id in $(aws apigateway get-rest-apis --region us-east-1 \
  --query 'items[*].id' --output text); do
  stage_count=$(aws apigateway get-stages \
    --rest-api-id $api_id --region us-east-1 \
    --query 'length(item)' --output text 2>/dev/null || echo "0")
  if [ "$stage_count" = "0" ] || [ "$stage_count" = "None" ]; then
    echo "ORPHANED: $api_id"
  fi
done
```

---

*Document maintained by Platform Engineering. For questions or exceptions, contact platform-engineering@yourcompany.com.*
