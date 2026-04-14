"""
lambda_scanner.py
─────────────────
Lambda #1 — Scans all API Gateway APIs across all enabled regions,
pulls 90-day CloudWatch invocation counts, and upserts records into
the DynamoDB inventory table.

Triggered by: EventBridge Scheduler (weekly, Monday 08:00 UTC)
IAM role needs: apigateway:GET, apigatewayv2:GET,
                cloudwatch:GetMetricStatistics, dynamodb:PutItem,
                account:ListRegions
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


# ── Entry point ──────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    regions = get_enabled_regions()
    logger.info(f"Scanning {len(regions)} region(s): {regions}")

    inventory = []
    for region in regions:
        try:
            inventory.extend(scan_rest_apis(region))
            inventory.extend(scan_v2_apis(region))
        except Exception as e:
            logger.error(f"Error scanning region {region}: {e}")

    save_to_dynamodb(inventory)
    logger.info(f"Upserted {len(inventory)} API record(s) into {DYNAMODB_TABLE}")
    return {"scanned": len(inventory), "regions": regions}


# ── Region discovery ─────────────────────────────────────────────────────────

def get_enabled_regions():
    client = boto3.client("account")
    paginator = client.get_paginator("list_regions")
    regions = []
    for page in paginator.paginate(RegionOptStatusContains=["ENABLED"]):
        regions.extend([r["RegionName"] for r in page["Regions"]])
    return regions


# ── REST API (v1) scanner ────────────────────────────────────────────────────

def scan_rest_apis(region: str) -> list:
    apigw = boto3.client("apigateway", region_name=region)
    cw = boto3.client("cloudwatch", region_name=region)
    results = []

    paginator = apigw.get_paginator("get_rest_apis")
    for page in paginator.paginate():
        for api in page.get("items", []):
            api_id = api["id"]
            api_name = api["name"]

            # Skip protected APIs
            tags = api.get("tags", {})
            if is_protected(tags):
                logger.info(f"Skipping protected REST API: {api_id} ({api_name})")
                continue

            count = get_invocation_count(cw, api_name=api_name, api_id=None)
            last_invocation = get_last_invocation_date(cw, api_name=api_name)
            has_stages = check_has_stages(apigw, api_id)

            results.append({
                "api_id": api_id,
                "region": region,
                "account_id": get_account_id(),
                "api_name": api_name,
                "protocol": "REST",
                "created_date": safe_isoformat(api.get("createdDate")),
                "invocation_count_90d": Decimal(str(count)),
                "last_invocation": last_invocation,
                "has_stages": has_stages,
                "tags": tags,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            })

    return results


# ── HTTP / WebSocket API (v2) scanner ────────────────────────────────────────

def scan_v2_apis(region: str) -> list:
    apigwv2 = boto3.client("apigatewayv2", region_name=region)
    cw = boto3.client("cloudwatch", region_name=region)
    results = []

    paginator = apigwv2.get_paginator("get_apis")
    for page in paginator.paginate():
        for api in page.get("Items", []):
            api_id = api["ApiId"]
            api_name = api["Name"]

            tags = api.get("Tags", {})
            if is_protected(tags):
                logger.info(f"Skipping protected v2 API: {api_id} ({api_name})")
                continue

            count = get_invocation_count(cw, api_name=api_name, api_id=api_id)
            last_invocation = get_last_invocation_date(cw, api_name=api_name)

            results.append({
                "api_id": api_id,
                "region": region,
                "account_id": get_account_id(),
                "api_name": api_name,
                "protocol": api.get("ProtocolType", "HTTP"),
                "created_date": safe_isoformat(api.get("CreatedDate")),
                "invocation_count_90d": Decimal(str(count)),
                "last_invocation": last_invocation,
                "has_stages": True,  # v2 APIs always have a $default stage
                "tags": tags,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            })

    return results


# ── CloudWatch helpers ───────────────────────────────────────────────────────

def get_invocation_count(cw_client, api_name: str, api_id: str | None) -> int:
    """Return total invocation Count over the look-back window."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)

    dimensions = [{"Name": "ApiName", "Value": api_name}]
    if api_id:
        dimensions.append({"Name": "ApiId", "Value": api_id})

    try:
        response = cw_client.get_metric_statistics(
            Namespace="AWS/ApiGateway",
            MetricName="Count",
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=LOOKBACK_DAYS * 86400,
            Statistics=["Sum"],
        )
        datapoints = response.get("Datapoints", [])
        return int(datapoints[0]["Sum"]) if datapoints else 0
    except Exception as e:
        logger.warning(f"CloudWatch query failed for {api_name}: {e}")
        return 0


def get_last_invocation_date(cw_client, api_name: str) -> str:
    """Return ISO8601 timestamp of the last non-zero data point, or 'never'."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)

    try:
        response = cw_client.get_metric_statistics(
            Namespace="AWS/ApiGateway",
            MetricName="Count",
            Dimensions=[{"Name": "ApiName", "Value": api_name}],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Sum"],
        )
        datapoints = sorted(
            [dp for dp in response.get("Datapoints", []) if dp["Sum"] > 0],
            key=lambda x: x["Timestamp"],
            reverse=True,
        )
        return datapoints[0]["Timestamp"].isoformat() if datapoints else "never"
    except Exception:
        return "unknown"


# ── Helpers ──────────────────────────────────────────────────────────────────

_ACCOUNT_ID_CACHE = None

def get_account_id() -> str:
    global _ACCOUNT_ID_CACHE
    if not _ACCOUNT_ID_CACHE:
        _ACCOUNT_ID_CACHE = boto3.client("sts").get_caller_identity()["Account"]
    return _ACCOUNT_ID_CACHE


def check_has_stages(apigw_client, api_id: str) -> bool:
    try:
        response = apigw_client.get_stages(restApiId=api_id)
        return len(response.get("item", [])) > 0
    except Exception:
        return False


def is_protected(tags: dict) -> bool:
    protected = {
        "lifecycle": "protected",
        "do-not-delete": "true",
    }
    return any(tags.get(k) == v for k, v in protected.items())


def safe_isoformat(dt) -> str:
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def save_to_dynamodb(records: list):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(DYNAMODB_TABLE)
    with table.batch_writer() as batch:
        for record in records:
            batch.put_item(Item=record)
