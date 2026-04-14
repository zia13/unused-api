#!/usr/bin/env python3
"""
scan.py
────────
CLI script — runs a full scan + classification offline (no Lambda needed).
Outputs a CSV and a JSON report of all API Gateway APIs across all regions.

Usage:
    python scan.py [--regions us-east-1,eu-west-1] [--days 90] [--output ./report]

Requirements:
    pip install boto3 pyyaml
    AWS credentials configured (env vars, ~/.aws/credentials, or instance role)
"""

import argparse
import boto3
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Scan AWS API Gateway for unused APIs"
    )
    parser.add_argument(
        "--regions",
        help="Comma-separated list of regions (default: all enabled regions)",
        default=None,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Look-back window in days (default: 90)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=10,
        help="Low-traffic threshold in req/day (default: 10)",
    )
    parser.add_argument(
        "--output",
        default="./api-cleanup-report",
        help="Output path prefix for CSV and JSON files",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS CLI profile to use",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.profile:
        boto3.setup_default_session(profile_name=args.profile)

    regions = args.regions.split(",") if args.regions else get_enabled_regions()
    print(f"🔍  Scanning {len(regions)} region(s) with {args.days}-day look-back...")

    inventory = []
    for region in regions:
        print(f"  → {region}", end="", flush=True)
        rest = scan_rest_apis(region, args.days)
        v2 = scan_v2_apis(region, args.days)
        inventory.extend(rest + v2)
        print(f" ({len(rest)} REST, {len(v2)} v2)")

    # Classify
    for record in inventory:
        record["tier"] = classify(record, args.days, args.threshold)

    # Summary
    tiers = {"ACTIVE": 0, "LOW_TRAFFIC": 0, "DORMANT": 0, "ORPHANED": 0}
    for r in inventory:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1

    print("\n📊  Classification Summary:")
    for tier, count in tiers.items():
        print(f"    {tier:<15} {count}")

    # Write outputs
    write_csv(inventory, args.output + ".csv")
    write_json(inventory, args.output + ".json")
    print(f"\n✅  Reports written to {args.output}.csv and {args.output}.json")


# ── Scanning ──────────────────────────────────────────────────────────────────

def get_enabled_regions():
    try:
        client = boto3.client("account")
        paginator = client.get_paginator("list_regions")
        regions = []
        for page in paginator.paginate(RegionOptStatusContains=["ENABLED"]):
            regions.extend([r["RegionName"] for r in page["Regions"]])
        return regions
    except Exception:
        # Fallback to common regions if account API is not available
        return [
            "us-east-1", "us-east-2", "us-west-1", "us-west-2",
            "eu-west-1", "eu-west-2", "eu-central-1",
            "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
        ]


def scan_rest_apis(region: str, days: int) -> list:
    try:
        apigw = boto3.client("apigateway", region_name=region)
        cw = boto3.client("cloudwatch", region_name=region)
        results = []

        paginator = apigw.get_paginator("get_rest_apis")
        for page in paginator.paginate():
            for api in page.get("items", []):
                api_id = api["id"]
                count = get_invocation_count(cw, api["name"], days)
                last_inv = get_last_invocation_date(cw, api["name"], days)
                has_stages = check_has_stages(apigw, api_id)

                results.append({
                    "api_id": api_id,
                    "api_name": api["name"],
                    "protocol": "REST",
                    "region": region,
                    "created_date": safe_str(api.get("createdDate")),
                    "invocation_count_90d": count,
                    "last_invocation": last_inv,
                    "has_stages": has_stages,
                    "owner_tag": api.get("tags", {}).get("owner", ""),
                    "team_tag": api.get("tags", {}).get("team", ""),
                    "lifecycle_tag": api.get("tags", {}).get("lifecycle", ""),
                    "tags": json.dumps(api.get("tags", {})),
                })
        return results
    except Exception as e:
        print(f"\n    ⚠️  Error scanning REST APIs in {region}: {e}")
        return []


def scan_v2_apis(region: str, days: int) -> list:
    try:
        apigwv2 = boto3.client("apigatewayv2", region_name=region)
        cw = boto3.client("cloudwatch", region_name=region)
        results = []

        paginator = apigwv2.get_paginator("get_apis")
        for page in paginator.paginate():
            for api in page.get("Items", []):
                api_id = api["ApiId"]
                count = get_invocation_count(cw, api["Name"], days)
                last_inv = get_last_invocation_date(cw, api["Name"], days)

                results.append({
                    "api_id": api_id,
                    "api_name": api["Name"],
                    "protocol": api.get("ProtocolType", "HTTP"),
                    "region": region,
                    "created_date": safe_str(api.get("CreatedDate")),
                    "invocation_count_90d": count,
                    "last_invocation": last_inv,
                    "has_stages": True,
                    "owner_tag": api.get("Tags", {}).get("owner", ""),
                    "team_tag": api.get("Tags", {}).get("team", ""),
                    "lifecycle_tag": api.get("Tags", {}).get("lifecycle", ""),
                    "tags": json.dumps(api.get("Tags", {})),
                })
        return results
    except Exception as e:
        print(f"\n    ⚠️  Error scanning v2 APIs in {region}: {e}")
        return []


# ── CloudWatch ────────────────────────────────────────────────────────────────

def get_invocation_count(cw, api_name: str, days: int) -> int:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/ApiGateway",
            MetricName="Count",
            Dimensions=[{"Name": "ApiName", "Value": api_name}],
            StartTime=start,
            EndTime=end,
            Period=days * 86400,
            Statistics=["Sum"],
        )
        dp = resp.get("Datapoints", [])
        return int(dp[0]["Sum"]) if dp else 0
    except Exception:
        return 0


def get_last_invocation_date(cw, api_name: str, days: int) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/ApiGateway",
            MetricName="Count",
            Dimensions=[{"Name": "ApiName", "Value": api_name}],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Sum"],
        )
        dp = sorted(
            [d for d in resp.get("Datapoints", []) if d["Sum"] > 0],
            key=lambda x: x["Timestamp"],
            reverse=True,
        )
        return dp[0]["Timestamp"].strftime("%Y-%m-%d") if dp else "never"
    except Exception:
        return "unknown"


# ── Classification ────────────────────────────────────────────────────────────

def classify(record: dict, days: int, threshold: int) -> str:
    count = record.get("invocation_count_90d", 0)
    has_stages = record.get("has_stages", False)
    last_inv = record.get("last_invocation", "never")

    if not has_stages or (count == 0 and last_inv == "never"):
        return "ORPHANED"

    if count == 0:
        return "DORMANT"

    avg_daily = count / days
    if avg_daily < threshold:
        return "LOW_TRAFFIC" if _within_days(last_inv, 30) else "DORMANT"

    return "ACTIVE"


def _within_days(date_str: str, days: int) -> bool:
    if date_str in ("never", "unknown", ""):
        return False
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
    except ValueError:
        return False


# ── Output helpers ────────────────────────────────────────────────────────────

def write_csv(records: list, path: str):
    if not records:
        return
    fields = [
        "tier", "api_id", "api_name", "protocol", "region",
        "created_date", "invocation_count_90d", "last_invocation",
        "has_stages", "owner_tag", "team_tag", "lifecycle_tag",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        # Sort by tier severity
        order = {"ORPHANED": 0, "DORMANT": 1, "LOW_TRAFFIC": 2, "ACTIVE": 3}
        for record in sorted(records, key=lambda r: order.get(r["tier"], 9)):
            writer.writerow(record)


def write_json(records: list, path: str):
    with open(path, "w") as f:
        json.dump(records, f, indent=2, default=str)


def safe_str(val) -> str:
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def check_has_stages(apigw_client, api_id: str) -> bool:
    try:
        response = apigw_client.get_stages(restApiId=api_id)
        return len(response.get("item", [])) > 0
    except Exception:
        return False


if __name__ == "__main__":
    main()
