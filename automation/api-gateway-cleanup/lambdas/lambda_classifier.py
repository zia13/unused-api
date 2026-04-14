"""
lambda_classifier.py
─────────────────────
Lambda #2 — Reads all API records from DynamoDB, applies the
four-tier classification rules, and writes the tier back to the table.

Tiers:
  ACTIVE       — traffic in the last 30 days
  LOW_TRAFFIC  — < LOW_TRAFFIC_THRESHOLD req/day over 90 days
  DORMANT      — zero traffic 30–90 days, has a live stage
  ORPHANED     — no stage, no deployment, or zero traffic ever

Triggered by: EventBridge Scheduler (weekly, Monday 09:00 UTC)
              OR directly after lambda_scanner completes (Step Functions)
IAM role needs: dynamodb:Scan, dynamodb:UpdateItem
"""

import os
import boto3
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "api-gateway-inventory")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "90"))
LOW_TRAFFIC_THRESHOLD = int(os.environ.get("LOW_TRAFFIC_THRESHOLD", "10"))  # req/day
DORMANT_DAYS = int(os.environ.get("DORMANT_DAYS", "30"))


# ── Tier constants ────────────────────────────────────────────────────────────

ACTIVE = "ACTIVE"
LOW_TRAFFIC = "LOW_TRAFFIC"
DORMANT = "DORMANT"
ORPHANED = "ORPHANED"


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    records = fetch_all_records()
    logger.info(f"Classifying {len(records)} API record(s)")

    counts = {ACTIVE: 0, LOW_TRAFFIC: 0, DORMANT: 0, ORPHANED: 0}
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(DYNAMODB_TABLE)

    for record in records:
        tier = classify(record)
        counts[tier] += 1
        update_tier(table, record["api_id"], record["region"], tier)

    logger.info(f"Classification results: {counts}")
    return counts


# ── Classification logic ──────────────────────────────────────────────────────

def classify(record: dict) -> str:
    count_90d = int(record.get("invocation_count_90d", 0))
    has_stages = record.get("has_stages", False)
    last_invocation = record.get("last_invocation", "never")

    # ORPHANED: never had a stage or never had any traffic at all
    if not has_stages:
        return ORPHANED

    if count_90d == 0 and last_invocation == "never":
        return ORPHANED

    # DORMANT: had a stage but zero traffic for DORMANT_DAYS+
    if count_90d == 0:
        return DORMANT

    # LOW_TRAFFIC: less than threshold req/day on average
    avg_daily = count_90d / LOOKBACK_DAYS
    if avg_daily < LOW_TRAFFIC_THRESHOLD:
        # Check if traffic is recent (last 30 days)
        if _last_traffic_within_days(last_invocation, 30):
            return LOW_TRAFFIC
        else:
            return DORMANT

    return ACTIVE


def _last_traffic_within_days(last_invocation: str, days: int) -> bool:
    if last_invocation in ("never", "unknown", ""):
        return False
    try:
        dt = datetime.fromisoformat(last_invocation.replace("Z", "+00:00"))
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
    except ValueError:
        return False


# ── DynamoDB helpers ──────────────────────────────────────────────────────────

def fetch_all_records() -> list:
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(DYNAMODB_TABLE)
    records = []
    scan_kwargs = {}

    while True:
        response = table.scan(**scan_kwargs)
        records.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return records


def update_tier(table, api_id: str, region: str, tier: str):
    try:
        table.update_item(
            Key={"api_id": api_id, "region": region},
            UpdateExpression="SET tier = :t, classified_at = :ca",
            ExpressionAttributeValues={
                ":t": tier,
                ":ca": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        logger.error(f"Failed to update tier for {api_id}/{region}: {e}")
