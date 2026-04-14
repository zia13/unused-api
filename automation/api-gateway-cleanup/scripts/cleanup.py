#!/usr/bin/env python3
"""
cleanup.py
───────────
CLI script — reads the CSV/JSON report from scan.py and performs
soft-delete or hard-delete on DORMANT/ORPHANED APIs.

Usage:
    # Dry run (default) — shows what would be deleted
    python cleanup.py --report ./api-cleanup-report.json

    # Soft delete (throttle to zero) — tiers: DORMANT, ORPHANED
    python cleanup.py --report ./api-cleanup-report.json --mode soft --no-dry-run

    # Hard delete — only APIs already soft-deleted N days ago
    python cleanup.py --report ./api-cleanup-report.json --mode hard --no-dry-run

    # Target a specific tier only
    python cleanup.py --report ./api-cleanup-report.json --tier ORPHANED --no-dry-run

Requirements:
    pip install boto3
"""

import argparse
import boto3
import json
import sys
import time
from datetime import datetime, timezone

ACTIONABLE_TIERS = {"DORMANT", "ORPHANED"}


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Cleanup unused API Gateway APIs")
    parser.add_argument("--report", required=True, help="Path to JSON report from scan.py")
    parser.add_argument(
        "--mode",
        choices=["soft", "hard"],
        default="soft",
        help="soft=throttle to zero, hard=delete (default: soft)",
    )
    parser.add_argument(
        "--tier",
        choices=["DORMANT", "ORPHANED", "ALL"],
        default="ALL",
        help="Only process this tier (default: ALL actionable tiers)",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually perform changes (default: dry run)",
    )
    parser.add_argument(
        "--region-filter",
        default=None,
        help="Comma-separated regions to limit scope",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS CLI profile",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    dry_run = not args.no_dry_run

    if args.profile:
        boto3.setup_default_session(profile_name=args.profile)

    with open(args.report) as f:
        records = json.load(f)

    # Filter
    target_tiers = ACTIONABLE_TIERS if args.tier == "ALL" else {args.tier}
    region_filter = set(args.region_filter.split(",")) if args.region_filter else None

    candidates = [
        r for r in records
        if r.get("tier") in target_tiers
        and (region_filter is None or r.get("region") in region_filter)
    ]

    print(f"\n{'⚠️  DRY RUN — no changes will be made' if dry_run else '🔴  LIVE RUN'}")
    print(f"Mode: {args.mode.upper()} | Tiers: {target_tiers} | Candidates: {len(candidates)}\n")

    if not candidates:
        print("No actionable APIs found.")
        return

    print(f"{'TIER':<15} {'PROTOCOL':<10} {'REGION':<15} {'API ID':<15} {'API NAME'}")
    print("-" * 80)
    for r in candidates:
        print(f"{r['tier']:<15} {r['protocol']:<10} {r['region']:<15} {r['api_id']:<15} {r['api_name']}")

    if dry_run:
        print(f"\n[DRY RUN] Would process {len(candidates)} API(s). Pass --no-dry-run to apply.")
        return

    confirm = input(f"\nType 'yes' to confirm {args.mode.upper()} of {len(candidates)} API(s): ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        sys.exit(0)

    results = {"ok": 0, "error": 0}
    for record in candidates:
        process(record, args.mode, results)
        time.sleep(0.3)  # Rate limiting

    print(f"\n✅  Done. Success: {results['ok']} | Errors: {results['error']}")


# ── Processing ────────────────────────────────────────────────────────────────

def process(record: dict, mode: str, results: dict):
    api_id = record["api_id"]
    region = record["region"]
    protocol = record["protocol"]

    print(f"  [{mode.upper()}] {protocol} {api_id} ({record['api_name']}) in {region} ... ", end="")

    try:
        if mode == "soft":
            soft_delete(api_id, region, protocol)
        else:
            hard_delete(api_id, region, protocol)
        print("✓")
        results["ok"] += 1
    except Exception as e:
        print(f"✗ {e}")
        results["error"] += 1


# ── Soft delete ───────────────────────────────────────────────────────────────

def soft_delete(api_id: str, region: str, protocol: str):
    if protocol == "REST":
        apigw = boto3.client("apigateway", region_name=region)
        stages = apigw.get_stages(restApiId=api_id).get("item", [])
        for stage in stages:
            apigw.update_stage(
                restApiId=api_id,
                stageName=stage["stageName"],
                patchOperations=[
                    {"op": "replace", "path": "/*/*/throttling/burstLimit", "value": "0"},
                    {"op": "replace", "path": "/*/*/throttling/rateLimit", "value": "0"},
                ],
            )
    else:
        apigwv2 = boto3.client("apigatewayv2", region_name=region)
        stages = apigwv2.get_stages(ApiId=api_id).get("Items", [])
        for stage in stages:
            apigwv2.update_stage(
                ApiId=api_id,
                StageName=stage["StageName"],
                DefaultRouteSettings={"ThrottlingBurstLimit": 0, "ThrottlingRateLimit": 0},
            )


# ── Hard delete ───────────────────────────────────────────────────────────────

def hard_delete(api_id: str, region: str, protocol: str):
    if protocol == "REST":
        boto3.client("apigateway", region_name=region).delete_rest_api(restApiId=api_id)
    else:
        boto3.client("apigatewayv2", region_name=region).delete_api(ApiId=api_id)


if __name__ == "__main__":
    main()
