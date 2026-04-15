#!/usr/bin/env python3
"""
provision_test_apis.py
───────────────────────
Creates 3 test REST APIs in API Gateway that exercise every tier
the automation classifies:

  test-api-active    → has a stage + will be seeded with recent traffic
  test-api-dormant   → has a stage  but no traffic (zero invocations)
  test-api-orphaned  → NO stages / NO deployment  (never callable)

Usage:
    # Create the 3 APIs and print their IDs
    python scripts/provision_test_apis.py --region us-east-1

    # Tear them all down
    python scripts/provision_test_apis.py --region us-east-1 --delete

    # Save the API IDs to a JSON file for use by the e2e test
    python scripts/provision_test_apis.py --region us-east-1 --output test-apis.json

Requirements:
    pip install boto3
"""

import argparse
import boto3
import json
import sys
import time
from datetime import datetime, timezone

# ── Fixtures ──────────────────────────────────────────────────────────────────

TEST_APIS = [
    {
        "key":         "active",
        "name":        "test-api-active",
        "description": "E2E test — ACTIVE tier (has stage, has traffic via direct invoke)",
        "add_stage":   True,
        "expected_tier": "ACTIVE",
    },
    {
        "key":         "dormant",
        "name":        "test-api-dormant",
        "description": "E2E test — DORMANT tier (has stage, zero traffic)",
        "add_stage":   True,
        "expected_tier": "DORMANT",
    },
    {
        "key":         "orphaned",
        "name":        "test-api-orphaned",
        "description": "E2E test — ORPHANED tier (no stage, no deployment)",
        "add_stage":   False,
        "expected_tier": "ORPHANED",
    },
]

PROTECTED_TAG = {"do-not-delete": "true"}   # used by the real automation to skip
TEST_TAG      = {"e2e-test": "true"}         # marker so we can find them later


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Provision/delete e2e test APIs")
    p.add_argument("--region",  default="us-east-1")
    p.add_argument("--profile", default=None)
    p.add_argument("--delete",  action="store_true", help="Delete all test APIs instead of creating")
    p.add_argument("--output",  default=None, help="Write created API IDs to this JSON file")
    return p.parse_args()


# ── Provision ─────────────────────────────────────────────────────────────────

def provision(region: str, output_path: str | None):
    apigw = boto3.client("apigateway", region_name=region)
    created = {}

    for fixture in TEST_APIS:
        name = fixture["name"]
        print(f"\n── Creating: {name} ─────────────────────────────────────")

        # ── 1. Create the REST API ────────────────────────────────────────────
        api = apigw.create_rest_api(
            name=name,
            description=fixture["description"],
            tags={**TEST_TAG, "expected-tier": fixture["expected_tier"]},
        )
        api_id = api["id"]
        print(f"   api_id        : {api_id}")

        # ── 2. Add a root resource + GET method ───────────────────────────────
        resources  = apigw.get_resources(restApiId=api_id)
        root_id    = resources["items"][0]["id"]

        apigw.put_method(
            restApiId=api_id,
            resourceId=root_id,
            httpMethod="GET",
            authorizationType="NONE",
        )

        apigw.put_integration(
            restApiId=api_id,
            resourceId=root_id,
            httpMethod="GET",
            type="MOCK",
            requestTemplates={"application/json": '{"statusCode": 200}'},
        )

        apigw.put_method_response(
            restApiId=api_id,
            resourceId=root_id,
            httpMethod="GET",
            statusCode="200",
        )

        apigw.put_integration_response(
            restApiId=api_id,
            resourceId=root_id,
            httpMethod="GET",
            statusCode="200",
            responseTemplates={"application/json": '{"message": "ok"}'},
        )

        # ── 3. Create a deployment + stage (only for active & dormant) ────────
        stage_name = None
        invoke_url = None
        if fixture["add_stage"]:
            deployment = apigw.create_deployment(restApiId=api_id)
            deployment_id = deployment["id"]

            stage_name = "test"
            apigw.create_stage(
                restApiId=api_id,
                stageName=stage_name,
                deploymentId=deployment_id,
                description=f"E2E test stage for {name}",
            )
            invoke_url = f"https://{api_id}.execute-api.{region}.amazonaws.com/{stage_name}"
            print(f"   stage         : {stage_name}")
            print(f"   invoke_url    : {invoke_url}")
        else:
            print(f"   stage         : NONE (intentionally orphaned)")

        created[fixture["key"]] = {
            "api_id":        api_id,
            "api_name":      name,
            "region":        region,
            "stage":         stage_name,
            "invoke_url":    invoke_url,
            "expected_tier": fixture["expected_tier"],
        }
        print(f"   expected tier : {fixture['expected_tier']}")
        print(f"   ✅ Done")

    # ── 4. Warm up the ACTIVE API (call it directly via boto3 test-invoke) ─────
    print("\n── Seeding traffic on test-api-active ───────────────────────────────")
    active = created["active"]
    resources  = apigw.get_resources(restApiId=active["api_id"])
    root_id    = resources["items"][0]["id"]
    for i in range(5):
        try:
            apigw.test_invoke_method(
                restApiId=active["api_id"],
                resourceId=root_id,
                httpMethod="GET",
            )
        except Exception as e:
            print(f"   invoke #{i+1} warning: {e}")
    print("   ✅ 5 test invocations sent (CloudWatch may take ~5 min to reflect)")

    # ── 5. Print summary ──────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  TEST API SUMMARY")
    print("═" * 60)
    for key, info in created.items():
        print(f"  {info['api_name']:<25} id={info['api_id']}  tier={info['expected_tier']}")
    print("═" * 60)

    if output_path:
        with open(output_path, "w") as f:
            json.dump(created, f, indent=2)
        print(f"\n✅ API IDs written to: {output_path}")

    return created


# ── Deprovision ───────────────────────────────────────────────────────────────

def delete(region: str):
    apigw = boto3.client("apigateway", region_name=region)

    # Find all APIs tagged e2e-test=true
    paginator = apigw.get_paginator("get_rest_apis")
    to_delete = []
    for page in paginator.paginate():
        for api in page.get("items", []):
            if api.get("tags", {}).get("e2e-test") == "true":
                to_delete.append(api)

    if not to_delete:
        print("No e2e test APIs found.")
        return

    print(f"Found {len(to_delete)} test API(s) to delete:")
    for api in to_delete:
        print(f"  {api['name']}  ({api['id']})")

    for api in to_delete:
        try:
            apigw.delete_rest_api(restApiId=api["id"])
            print(f"  ✅ Deleted: {api['name']} ({api['id']})")
            # API Gateway enforces a 30-second cool-down between REST API deletes
            time.sleep(31)
        except Exception as e:
            print(f"  ❌ Failed to delete {api['name']}: {e}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.profile:
        boto3.setup_default_session(profile_name=args.profile)

    print(f"Region  : {args.region}")
    print(f"Action  : {'DELETE' if args.delete else 'PROVISION'}")
    print(f"Account : {boto3.client('sts').get_caller_identity()['Account']}")
    print()

    if args.delete:
        delete(args.region)
    else:
        provision(args.region, args.output)


if __name__ == "__main__":
    main()
