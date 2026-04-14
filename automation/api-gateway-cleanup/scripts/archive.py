#!/usr/bin/env python3
"""
archive.py
───────────
CLI script — exports OpenAPI specs for all REST APIs in a report and
uploads them to an S3 archive bucket before deletion.

Usage:
    python archive.py \
        --report ./api-cleanup-report.json \
        --bucket your-api-archive-bucket \
        [--tier DORMANT,ORPHANED] \
        [--profile myprofile]

Requirements:
    pip install boto3
"""

import argparse
import boto3
import json
import sys
from datetime import datetime, timezone


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Archive API Gateway specs to S3")
    parser.add_argument("--report", required=True, help="JSON report from scan.py")
    parser.add_argument("--bucket", required=True, help="S3 bucket name for archives")
    parser.add_argument(
        "--tier",
        default="DORMANT,ORPHANED",
        help="Comma-separated tiers to archive (default: DORMANT,ORPHANED)",
    )
    parser.add_argument("--profile", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be uploaded without actually uploading",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.profile:
        boto3.setup_default_session(profile_name=args.profile)

    target_tiers = set(args.tier.split(","))

    with open(args.report) as f:
        records = json.load(f)

    candidates = [r for r in records if r.get("tier") in target_tiers]
    print(f"{'[DRY RUN] ' if args.dry_run else ''}Archiving {len(candidates)} API(s) to s3://{args.bucket}/\n")

    sts = boto3.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    archived = 0
    skipped = 0

    for record in candidates:
        api_id = record["api_id"]
        region = record["region"]
        protocol = record["protocol"]
        api_name = record["api_name"]

        print(f"  {protocol} {api_id} ({api_name}) [{region}] ... ", end="")

        if protocol == "REST":
            ok = archive_rest_api(
                api_id, region, account_id, date_str, args.bucket, args.dry_run
            )
        else:
            ok = archive_metadata(
                record, account_id, date_str, args.bucket, args.dry_run
            )

        if ok:
            print("✓")
            archived += 1
        else:
            print("⚠  skipped (no stage or export failed)")
            skipped += 1

    print(f"\nDone. Archived: {archived} | Skipped: {skipped}")


# ── Archival helpers ──────────────────────────────────────────────────────────

def archive_rest_api(
    api_id: str,
    region: str,
    account_id: str,
    date_str: str,
    bucket: str,
    dry_run: bool,
) -> bool:
    apigw = boto3.client("apigateway", region_name=region)
    s3 = boto3.client("s3")

    try:
        stages = apigw.get_stages(restApiId=api_id).get("item", [])
        if not stages:
            return False
        stage_name = stages[0]["stageName"]

        for fmt, export_type in [("oas30", "oas30"), ("swagger", "swagger")]:
            try:
                export = apigw.get_export(
                    restApiId=api_id,
                    stageName=stage_name,
                    exportType=export_type,
                    accepts="application/json",
                )
                spec = export["body"].read()
                key = f"api-gateway/{account_id}/{region}/{api_id}/{date_str}-{fmt}.json"

                if dry_run:
                    print(f"\n    [DRY RUN] Would upload to s3://{bucket}/{key}")
                else:
                    s3.put_object(
                        Bucket=bucket,
                        Key=key,
                        Body=spec,
                        ContentType="application/json",
                        ServerSideEncryption="AES256",
                        Tagging=f"api_id={api_id}&region={region}&archived_date={date_str}",
                    )
            except Exception as e:
                print(f"\n    ⚠️  {fmt} export failed: {e}")

        return True

    except Exception as e:
        print(f"\n    ✗ {e}")
        return False


def archive_metadata(
    record: dict,
    account_id: str,
    date_str: str,
    bucket: str,
    dry_run: bool,
) -> bool:
    s3 = boto3.client("s3")
    api_id = record["api_id"]
    region = record["region"]
    key = f"api-gateway/{account_id}/{region}/{api_id}/{date_str}-metadata.json"
    payload = json.dumps(record, indent=2, default=str).encode()

    if dry_run:
        print(f"\n    [DRY RUN] Would upload metadata to s3://{bucket}/{key}")
        return True

    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        return True
    except Exception as e:
        print(f"\n    ✗ {e}")
        return False


if __name__ == "__main__":
    main()
