"""
lambda_notifier.py
───────────────────
Lambda #3 — Queries DynamoDB for DORMANT and ORPHANED APIs that
have not yet been notified, sends an SES email to the owner, and
records the notification timestamp.

Triggered by: EventBridge Scheduler (weekly, Monday 10:00 UTC)
IAM role needs: dynamodb:Scan, dynamodb:UpdateItem,
                ses:SendEmail, apigateway:GET, apigatewayv2:GET
"""

import os
import boto3
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "api-gateway-inventory")
SES_SENDER = os.environ.get("SES_SENDER_EMAIL", "mdziaur.rahman@corebridgefinancial.com")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
NOTICE_PERIOD_DAYS = int(os.environ.get("NOTICE_PERIOD_DAYS", "30"))
ESCALATION_DAYS = int(os.environ.get("ESCALATION_DAYS", "14"))


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    records = fetch_actionable_records()
    logger.info(f"Found {len(records)} API(s) requiring notification")

    notified = 0
    escalated = 0

    for record in records:
        owner_email = resolve_owner_email(record)
        if not owner_email:
            logger.warning(f"No owner found for {record['api_id']} — escalating to sender")
            owner_email = SES_SENDER

        notified_at = record.get("notified_at")
        if notified_at and not should_escalate(notified_at):
            # Already notified recently, skip
            continue

        if notified_at and should_escalate(notified_at):
            send_escalation_email(record, owner_email)
            escalated += 1
        else:
            send_initial_notification(record, owner_email)
            set_deletion_schedule(record)
            notified += 1

    logger.info(f"Sent {notified} initial notification(s), {escalated} escalation(s)")
    return {"notified": notified, "escalated": escalated}


# ── DynamoDB ──────────────────────────────────────────────────────────────────

def fetch_actionable_records() -> list:
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(DYNAMODB_TABLE)
    records = []
    scan_kwargs = {
        "FilterExpression": "tier IN (:dormant, :orphaned) AND attribute_not_exists(deleted_at)",
        "ExpressionAttributeValues": {":dormant": "DORMANT", ":orphaned": "ORPHANED"},
    }

    while True:
        response = table.scan(**scan_kwargs)
        records.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return records


def set_deletion_schedule(record: dict):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(DYNAMODB_TABLE)
    deletion_date = (
        datetime.now(timezone.utc) + timedelta(days=NOTICE_PERIOD_DAYS)
    ).isoformat()

    table.update_item(
        Key={"api_id": record["api_id"], "region": record["region"]},
        UpdateExpression="SET notified_at = :na, deletion_scheduled_date = :ds",
        ExpressionAttributeValues={
            ":na": datetime.now(timezone.utc).isoformat(),
            ":ds": deletion_date,
        },
    )


# ── Email helpers ─────────────────────────────────────────────────────────────

def send_initial_notification(record: dict, owner_email: str):
    deletion_date = (
        datetime.now(timezone.utc) + timedelta(days=NOTICE_PERIOD_DAYS)
    ).strftime("%B %d, %Y")

    escalation_date = (
        datetime.now(timezone.utc) + timedelta(days=ESCALATION_DAYS)
    ).strftime("%B %d, %Y")

    subject = (
        f"[ACTION REQUIRED] Unused API Gateway API scheduled for deletion"
        f" — {record['api_name']} ({record['api_id']})"
    )

    body = f"""Hi there,

Our automated API hygiene scan has identified the following API Gateway endpoint
as unused based on {os.environ.get('LOOKBACK_DAYS', '90')} days of CloudWatch metrics:

  API Name:    {record['api_name']}
  API ID:      {record['api_id']}
  Region:      {record['region']}
  Account:     {record['account_id']}
  Protocol:    {record['protocol']}
  Last seen:   {record.get('last_invocation', 'Never')}
  Tier:        {record['tier']}

Scheduled deletion date: {deletion_date}

Actions you can take before deletion:
  1. Reply to confirm this API can be deleted.
  2. Reply to request a 30-day extension (one extension allowed).
  3. Tag the API with 'lifecycle: protected' to permanently exclude it.

If no response is received by {escalation_date}, this will be escalated to your team lead.

Questions? Contact: {SES_SENDER}

---
This is an automated message from the Platform Engineering API cleanup pipeline.
"""
    _send_ses(to=owner_email, subject=subject, body=body)
    logger.info(f"Sent initial notification to {owner_email} for {record['api_id']}")


def send_escalation_email(record: dict, owner_email: str):
    subject = (
        f"[ESCALATION] No response — API Gateway deletion imminent"
        f" — {record['api_name']} ({record['api_id']})"
    )
    body = f"""Hi,

This is an escalation notice. No response has been received for the following
API Gateway endpoint flagged for deletion:

  API Name:  {record['api_name']}
  API ID:    {record['api_id']}
  Region:    {record['region']}
  Tier:      {record['tier']}
  Notified:  {record.get('notified_at', 'unknown')}

Deletion will proceed automatically unless you act now. Please reply or apply
the 'lifecycle: protected' tag to the API in the AWS console.

Contact: {SES_SENDER}
"""
    _send_ses(to=owner_email, subject=subject, body=body)
    # Also publish to SNS for the platform engineering channel
    if SNS_TOPIC_ARN:
        sns = boto3.client("sns")
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=body,
        )
    logger.info(f"Sent escalation for {record['api_id']}")


def _send_ses(to: str, subject: str, body: str):
    ses = boto3.client("ses")
    ses.send_email(
        Source=SES_SENDER,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        },
    )


# ── Owner resolution ──────────────────────────────────────────────────────────

def resolve_owner_email(record: dict) -> str | None:
    """Check tags first, then fall back to account-level contact."""
    tags = record.get("tags", {})
    if isinstance(tags, dict):
        return tags.get("owner") or tags.get("Owner") or tags.get("contact")
    return None


# ── Escalation check ──────────────────────────────────────────────────────────

def should_escalate(notified_at: str) -> bool:
    try:
        dt = datetime.fromisoformat(notified_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - dt >= timedelta(days=ESCALATION_DAYS)
    except ValueError:
        return False
