"""
lambda_cleaner.py
──────────────────
Lambda #4 — The deletion engine. Processes APIs whose:
  - deletion_scheduled_date has passed, AND
  - approved_for_deletion == True  (or notice period has fully elapsed)

Steps per API:
  1. Export & archive OpenAPI spec to S3
  2. Throttle stage to zero (soft delete)
  3. Wait SOFT_DELETE_WINDOW_DAYS (handled externally via Step Functions wait state)
  4. Hard delete the API
  5. Mark record deleted_at in DynamoDB

DRY_RUN=true  → logs actions but never calls delete/throttle APIs.

Triggered by: Step Functions state machine (daily check) OR manual invocation
IAM role needs: apigateway:DELETE, apigateway:GET, apigateway:PATCH,
                apigatewayv2:DELETE, apigatewayv2:GET,
                dynamodb:Scan, dynamodb:UpdateItem,
                s3:PutObject
"""

import os
import json
import boto3
import logging
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "api-gateway-inventory")
S3_ARCHIVE_BUCKET = os.environ.get("S3_ARCHIVE_BUCKET", "your-api-archive-bucket")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
SOFT_DELETE_WINDOW_DAYS = int(os.environ.get("SOFT_DELETE_WINDOW_DAYS", "7"))

# Cleaner modes: "soft" throttles to zero, "hard" deletes
MODE = os.environ.get("CLEANER_MODE", "soft")  # "soft" | "hard"


# ── Entry point ───────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    if DRY_RUN:
        logger.warning("⚠️  DRY_RUN=true — no real changes will be made")

    records = fetch_due_records()
    logger.info(f"Found {len(records)} API(s) due for {MODE} cleanup")

    results = {"processed": 0, "errors": 0, "skipped": 0}
    for record in records:
        try:
            process(record, results)
        except Exception as e:
            logger.error(f"Error processing {record['api_id']}: {e}")
            results["errors"] += 1

    logger.info(f"Cleaner results: {results}")
    return results


# ── Record selection ──────────────────────────────────────────────────────────

def fetch_due_records() -> list:
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(DYNAMODB_TABLE)
    now_iso = datetime.now(timezone.utc).isoformat()

    records = []
    scan_kwargs = {
        "FilterExpression": (
            "tier IN (:dormant, :orphaned) "
            "AND attribute_not_exists(deleted_at) "
            "AND deletion_scheduled_date <= :now"
        ),
        "ExpressionAttributeValues": {
            ":dormant": "DORMANT",
            ":orphaned": "ORPHANED",
            ":now": now_iso,
        },
    }

    while True:
        response = table.scan(**scan_kwargs)
        records.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return records


# ── Processing logic ──────────────────────────────────────────────────────────

def process(record: dict, results: dict):
    api_id = record["api_id"]
    region = record["region"]
    protocol = record.get("protocol", "REST")
    api_name = record.get("api_name", api_id)

    logger.info(f"Processing [{MODE}] {protocol} API: {api_id} ({api_name}) in {region}")

    if MODE == "soft":
        archive_api_spec(record)
        soft_delete(api_id, region, protocol)
        mark_soft_deleted(api_id, region)
        results["processed"] += 1

    elif MODE == "hard":
        # Only hard-delete if soft-delete window has passed
        soft_deleted_at = record.get("soft_deleted_at")
        if not soft_delete_window_passed(soft_deleted_at):
            logger.info(
                f"Skipping hard delete for {api_id} — soft-delete window not elapsed"
            )
            results["skipped"] += 1
            return

        hard_delete(api_id, region, protocol)
        mark_hard_deleted(api_id, region)
        results["processed"] += 1


# ── Soft delete (throttle to zero) ───────────────────────────────────────────

def soft_delete(api_id: str, region: str, protocol: str):
    if protocol == "REST":
        _throttle_rest_api(api_id, region)
    else:
        _throttle_v2_api(api_id, region)


def _throttle_rest_api(api_id: str, region: str):
    apigw = boto3.client("apigateway", region_name=region)
    try:
        stages = apigw.get_stages(restApiId=api_id).get("item", [])
    except Exception as e:
        logger.warning(f"Could not list stages for {api_id}: {e}")
        return

    for stage in stages:
        stage_name = stage["stageName"]
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would throttle REST {api_id}/{stage_name} to 0")
            continue
        try:
            apigw.update_stage(
                restApiId=api_id,
                stageName=stage_name,
                patchOperations=[
                    {"op": "replace", "path": "/defaultRouteSettings/throttlingBurstLimit", "value": "0"},
                    {"op": "replace", "path": "/defaultRouteSettings/throttlingRateLimit", "value": "0"},
                ],
            )
            logger.info(f"Throttled REST {api_id}/{stage_name} to zero")
        except Exception as e:
            logger.warning(f"Throttle failed for {api_id}/{stage_name}: {e}")


def _throttle_v2_api(api_id: str, region: str):
    apigwv2 = boto3.client("apigatewayv2", region_name=region)
    try:
        stages = apigwv2.get_stages(ApiId=api_id).get("Items", [])
    except Exception as e:
        logger.warning(f"Could not list v2 stages for {api_id}: {e}")
        return

    for stage in stages:
        stage_name = stage["StageName"]
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would throttle v2 {api_id}/{stage_name} to 0")
            continue
        try:
            apigwv2.update_stage(
                ApiId=api_id,
                StageName=stage_name,
                DefaultRouteSettings={
                    "ThrottlingBurstLimit": 0,
                    "ThrottlingRateLimit": 0,
                },
            )
            logger.info(f"Throttled v2 {api_id}/{stage_name} to zero")
        except Exception as e:
            logger.warning(f"Throttle failed for v2 {api_id}/{stage_name}: {e}")


# ── Hard delete ───────────────────────────────────────────────────────────────

def hard_delete(api_id: str, region: str, protocol: str):
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would hard-delete {protocol} API: {api_id} in {region}")
        return

    if protocol == "REST":
        apigw = boto3.client("apigateway", region_name=region)
        apigw.delete_rest_api(restApiId=api_id)
        logger.info(f"Hard-deleted REST API: {api_id} in {region}")
    else:
        apigwv2 = boto3.client("apigatewayv2", region_name=region)
        apigwv2.delete_api(ApiId=api_id)
        logger.info(f"Hard-deleted v2 API: {api_id} in {region}")


# ── S3 archival ───────────────────────────────────────────────────────────────

def archive_api_spec(record: dict):
    api_id = record["api_id"]
    region = record["region"]
    account_id = record.get("account_id", "unknown")
    protocol = record.get("protocol", "REST")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if protocol != "REST":
        # v2 APIs don't support get-export; store the DynamoDB record as JSON
        _archive_json(record, api_id, region, account_id, date_str)
        return

    apigw = boto3.client("apigateway", region_name=region)
    s3 = boto3.client("s3")

    try:
        stages = apigw.get_stages(restApiId=api_id).get("item", [])
        stage_name = stages[0]["stageName"] if stages else "default"

        export = apigw.get_export(
            restApiId=api_id,
            stageName=stage_name,
            exportType="oas30",
            accepts="application/json",
        )
        spec_bytes = export["body"].read()

        s3_key = (
            f"api-gateway/{account_id}/{region}/{api_id}/{date_str}-oas30.json"
        )

        if DRY_RUN:
            logger.info(f"[DRY RUN] Would archive spec to s3://{S3_ARCHIVE_BUCKET}/{s3_key}")
        else:
            s3.put_object(
                Bucket=S3_ARCHIVE_BUCKET,
                Key=s3_key,
                Body=spec_bytes,
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
            logger.info(f"Archived spec to s3://{S3_ARCHIVE_BUCKET}/{s3_key}")
            _update_archive_key(api_id, region, s3_key)

    except Exception as e:
        logger.warning(f"Could not archive spec for {api_id}: {e}")
        _archive_json(record, api_id, region, account_id, date_str)


def _archive_json(record: dict, api_id: str, region: str, account_id: str, date_str: str):
    s3 = boto3.client("s3")
    s3_key = f"api-gateway/{account_id}/{region}/{api_id}/{date_str}-metadata.json"
    payload = json.dumps(record, default=str).encode()

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would archive metadata to s3://{S3_ARCHIVE_BUCKET}/{s3_key}")
        return

    s3.put_object(
        Bucket=S3_ARCHIVE_BUCKET,
        Key=s3_key,
        Body=payload,
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    _update_archive_key(api_id, region, s3_key)


def _update_archive_key(api_id: str, region: str, s3_key: str):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(DYNAMODB_TABLE)
    table.update_item(
        Key={"api_id": api_id, "region": region},
        UpdateExpression="SET archived_s3_key = :k",
        ExpressionAttributeValues={":k": s3_key},
    )


# ── DynamoDB state updates ────────────────────────────────────────────────────

def mark_soft_deleted(api_id: str, region: str):
    _update_field(api_id, region, "soft_deleted_at", datetime.now(timezone.utc).isoformat())


def mark_hard_deleted(api_id: str, region: str):
    _update_field(api_id, region, "deleted_at", datetime.now(timezone.utc).isoformat())


def _update_field(api_id: str, region: str, field: str, value: str):
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would set {field}={value} for {api_id}/{region}")
        return
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(DYNAMODB_TABLE)
    table.update_item(
        Key={"api_id": api_id, "region": region},
        UpdateExpression=f"SET {field} = :v",
        ExpressionAttributeValues={":v": value},
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def soft_delete_window_passed(soft_deleted_at: str | None) -> bool:
    if not soft_deleted_at:
        return False
    try:
        dt = datetime.fromisoformat(soft_deleted_at.replace("Z", "+00:00"))
        from datetime import timedelta
        return datetime.now(timezone.utc) - dt >= timedelta(days=SOFT_DELETE_WINDOW_DAYS)
    except ValueError:
        return False
