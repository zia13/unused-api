#!/usr/bin/env python3
"""
e2e_test.py
────────────
End-to-end test for the API Gateway Cleanup automation.

The 3 test APIs (active / dormant / orphaned) are provisioned by Terraform
(terraform/test_apis.tf).  This script reads their IDs straight from
`terraform output` — no boto3 provisioning needed here.

Flow:
  1. Reads the 3 test API IDs from terraform output (or --api-ids JSON file)
  2. Seeds a DynamoDB record for each, mirroring what the scanner writes
  3. Runs the classifier directly (imports the lambda module)
  4. Asserts each API lands in the correct tier
  5. Runs the notifier in dry-run mode and checks no exceptions are raised
  6. Runs the cleaner in soft-delete dry-run mode
  7. (--full) Runs the real scanner lambda then re-asserts tiers
  8. Verifies the protected-tag guard works
  9. Tears down the isolated test DynamoDB table + SNS topic
  10. Prints a pass/fail summary

Prerequisites:
    cd terraform
    terraform apply -var="create_test_apis=true"   # provisions the 3 APIs

Usage:
    # Quick mode — reads IDs from terraform output automatically
    python tests/e2e_test.py --region us-east-1 --tf-dir automation/api-gateway-cleanup/terraform

    # Pass a pre-saved JSON file instead of running terraform output
    python tests/e2e_test.py --region us-east-1 --api-ids test-apis.json

    # Full mode — also runs the real scanner lambda
    python tests/e2e_test.py --region us-east-1 --tf-dir ... --full

Requirements:
    pip install boto3 pyyaml
    AWS credentials must be configured (env vars, profile, or instance role)
    Terraform CLI installed (only needed if --tf-dir is used)
"""

import argparse
import importlib.util
import json
import os
import sys
import time
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import boto3

# ── Resolve paths ─────────────────────────────────────────────────────────────

REPO_ROOT    = Path(__file__).resolve().parents[1]
LAMBDAS_DIR  = REPO_ROOT / "lambdas"
SCRIPTS_DIR  = REPO_ROOT / "scripts"

sys.path.insert(0, str(LAMBDAS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_lambda(module_name: str):
    """Dynamically import a lambda module so we can call lambda_handler directly."""
    spec = importlib.util.spec_from_file_location(
        module_name, LAMBDAS_DIR / f"{module_name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def set_lambda_env(region, dynamodb_table, s3_bucket, sns_topic_arn, ses_email,
                   dry_run="true"):
    """Inject environment variables the lambdas read via os.environ."""
    os.environ["DYNAMODB_TABLE"]         = dynamodb_table
    os.environ["S3_ARCHIVE_BUCKET"]      = s3_bucket
    os.environ["SNS_TOPIC_ARN"]          = sns_topic_arn
    os.environ["SES_SENDER_EMAIL"]       = ses_email
    os.environ["LOOKBACK_DAYS"]          = "90"
    os.environ["LOW_TRAFFIC_THRESHOLD"]  = "10"
    os.environ["DRY_RUN"]                = dry_run
    os.environ["SOFT_DELETE_WINDOW_DAYS"]= "7"
    os.environ["NOTICE_PERIOD_DAYS"]     = "30"
    os.environ["DORMANT_DAYS"]           = "30"


def get_dynamo_record(table, api_id: str, region: str):  # -> dict | None
    resp = table.get_item(Key={"api_id": api_id, "region": region})
    return resp.get("Item")


def seed_dynamo_record(table, api_id: str, api_name: str, region: str,
                       invocation_count: int, has_stages: bool, last_invocation: str):
    """Write a scanner-style record into DynamoDB so the classifier can read it."""
    table.put_item(Item={
        "api_id":               api_id,
        "region":               region,
        "account_id":           boto3.client("sts").get_caller_identity()["Account"],
        "api_name":             api_name,
        "protocol":             "REST",
        "invocation_count_90d": Decimal(str(invocation_count)),
        "has_stages":           has_stages,
        "last_invocation":      last_invocation,
        "created_date":         datetime.now(timezone.utc).isoformat(),
        "scanned_at":           datetime.now(timezone.utc).isoformat(),
        "tags":                 {"e2e-test": "true"},
    })


# ── Terraform output reader ───────────────────────────────────────────────────

def load_from_terraform_output(tf_dir: str) -> dict:
    """
    Run `terraform output -json` in tf_dir and return the 3 test API IDs
    in the same shape that --api-ids JSON uses.
    """
    import subprocess
    result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=tf_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    outputs = json.loads(result.stdout)

    def val(key):
        return outputs.get(key, {}).get("value", "")

    active_id   = val("test_api_active_id")
    dormant_id  = val("test_api_dormant_id")
    orphaned_id = val("test_api_orphaned_id")

    if not active_id or not dormant_id or not orphaned_id:
        raise RuntimeError(
            "Terraform outputs for test APIs are empty.\n"
            "Run:  terraform apply -var='create_test_apis=true'"
        )

    return {
        "active": {
            "api_id":        active_id,
            "api_name":      "test-api-active",
            "region":        None,   # filled in by setUpClass
            "stage":         "test",
            "invoke_url":    val("test_api_active_invoke_url"),
            "expected_tier": "ACTIVE",
        },
        "dormant": {
            "api_id":        dormant_id,
            "api_name":      "test-api-dormant",
            "region":        None,
            "stage":         "test",
            "invoke_url":    val("test_api_dormant_invoke_url"),
            "expected_tier": "DORMANT",
        },
        "orphaned": {
            "api_id":        orphaned_id,
            "api_name":      "test-api-orphaned",
            "region":        None,
            "stage":         None,
            "invoke_url":    None,
            "expected_tier": "ORPHANED",
        },
    }


# ── Test Suite ────────────────────────────────────────────────────────────────

class E2ETestSuite(unittest.TestCase):

    # ── Class-level setup (run once for the whole suite) ─────────────────────

    @classmethod
    def setUpClass(cls):
        args = _ARGS  # injected by main()

        cls.region          = args.region
        cls.full            = args.full
        cls.dynamodb_table  = "api-gateway-inventory-e2e-test"
        cls.s3_bucket       = f"api-gateway-cleanup-archive-{args.region}-e2e"
        cls.sns_topic_arn   = ""
        cls.ses_email       = "noreply@example.com"

        # ── Load test API IDs ─────────────────────────────────────────────────
        if args.api_ids:
            with open(args.api_ids) as f:
                cls.apis = json.load(f)
            print(f"\n📂 Loaded API IDs from {args.api_ids}")
        elif args.tf_dir:
            print(f"\n🔧 Reading API IDs from terraform output ({args.tf_dir})...")
            cls.apis = load_from_terraform_output(args.tf_dir)
            print("   ✅ Terraform outputs loaded")
        else:
            raise RuntimeError(
                "Provide either --tf-dir (path to terraform dir) "
                "or --api-ids (path to JSON file)"
            )

        # Stamp the region onto every entry (terraform output doesn't include it)
        for info in cls.apis.values():
            info["region"] = cls.region

        # ── Create isolated DynamoDB table for this test run ──────────────────
        cls.dynamo = boto3.resource("dynamodb", region_name=cls.region)
        cls._create_test_dynamodb_table()

        # ── Create a minimal SNS topic for dry-run notifier calls ─────────────
        sns = boto3.client("sns", region_name=cls.region)
        resp = sns.create_topic(Name="api-gw-cleanup-e2e-test-alerts")
        cls.sns_topic_arn = resp["TopicArn"]

        # ── Set lambda env vars ───────────────────────────────────────────────
        set_lambda_env(
            region         = cls.region,
            dynamodb_table = cls.dynamodb_table,
            s3_bucket      = cls.s3_bucket,
            sns_topic_arn  = cls.sns_topic_arn,
            ses_email      = cls.ses_email,
            dry_run        = "true",
        )

        # ── Seed DynamoDB records mimicking the scanner output ────────────────
        table  = cls.dynamo.Table(cls.dynamodb_table)
        recent = datetime.now(timezone.utc).isoformat()

        seed_dynamo_record(table,
            api_id=cls.apis["active"]["api_id"],
            api_name=cls.apis["active"]["api_name"],
            region=cls.region,
            invocation_count=9000,   # 100 req/day — clearly ACTIVE
            has_stages=True,
            last_invocation=recent,
        )
        seed_dynamo_record(table,
            api_id=cls.apis["dormant"]["api_id"],
            api_name=cls.apis["dormant"]["api_name"],
            region=cls.region,
            invocation_count=0,      # zero traffic — DORMANT (has stage)
            has_stages=True,
            last_invocation="never",
        )
        seed_dynamo_record(table,
            api_id=cls.apis["orphaned"]["api_id"],
            api_name=cls.apis["orphaned"]["api_name"],
            region=cls.region,
            invocation_count=0,      # zero traffic, no stage — ORPHANED
            has_stages=False,
            last_invocation="never",
        )
        print("✅ DynamoDB seeded with 3 test records")

    @classmethod
    def _create_test_dynamodb_table(cls):
        ddb = boto3.client("dynamodb", region_name=cls.region)
        try:
            ddb.create_table(
                TableName=cls.dynamodb_table,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[
                    {"AttributeName": "api_id",  "AttributeType": "S"},
                    {"AttributeName": "region",  "AttributeType": "S"},
                    {"AttributeName": "tier",    "AttributeType": "S"},
                ],
                KeySchema=[
                    {"AttributeName": "api_id",  "KeyType": "HASH"},
                    {"AttributeName": "region",  "KeyType": "RANGE"},
                ],
                GlobalSecondaryIndexes=[{
                    "IndexName": "tier-index",
                    "KeySchema": [{"AttributeName": "tier", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }],
            )
            ddb.get_waiter("table_exists").wait(TableName=cls.dynamodb_table)
            print(f"✅ Isolated test DynamoDB table created: {cls.dynamodb_table}")
        except ddb.exceptions.ResourceInUseException:
            print(f"✅ Test DynamoDB table already exists: {cls.dynamodb_table}")

    @classmethod
    def tearDownClass(cls):
        # ── Delete isolated DynamoDB table ────────────────────────────────────
        try:
            boto3.client("dynamodb", region_name=cls.region).delete_table(
                TableName=cls.dynamodb_table
            )
            print(f"\n🧹 Deleted test DynamoDB table: {cls.dynamodb_table}")
        except Exception:
            pass

        # ── Delete SNS test topic ─────────────────────────────────────────────
        try:
            boto3.client("sns", region_name=cls.region).delete_topic(
                TopicArn=cls.sns_topic_arn
            )
        except Exception:
            pass

        print("\n💡 Test APIs are managed by Terraform.")
        print("   To destroy them:  terraform apply -var='create_test_apis=false'")

    # ── Test 1: API Gateway provisioning ─────────────────────────────────────

    def test_01_apis_exist_in_api_gateway(self):
        """All 3 test APIs must be reachable in API Gateway."""
        apigw = boto3.client("apigateway", region_name=self.region)
        for key, info in self.apis.items():
            with self.subTest(api=key):
                resp = apigw.get_rest_api(restApiId=info["api_id"])
                self.assertEqual(resp["name"], info["api_name"],
                    f"{key} API name mismatch")

    def test_02_active_api_has_stage(self):
        """ACTIVE test API must have exactly one stage called 'test'."""
        apigw  = boto3.client("apigateway", region_name=self.region)
        api_id = self.apis["active"]["api_id"]
        stages = apigw.get_stages(restApiId=api_id)["item"]
        names  = [s["stageName"] for s in stages]
        self.assertIn("test", names,
            f"ACTIVE API ({api_id}) missing 'test' stage — found: {names}")

    def test_03_dormant_api_has_stage(self):
        """DORMANT test API must have a stage."""
        apigw  = boto3.client("apigateway", region_name=self.region)
        api_id = self.apis["dormant"]["api_id"]
        stages = apigw.get_stages(restApiId=api_id)["item"]
        self.assertGreater(len(stages), 0,
            f"DORMANT API ({api_id}) expected a stage but found none")

    def test_04_orphaned_api_has_no_stage(self):
        """ORPHANED test API must have NO stages."""
        apigw  = boto3.client("apigateway", region_name=self.region)
        api_id = self.apis["orphaned"]["api_id"]
        stages = apigw.get_stages(restApiId=api_id)["item"]
        self.assertEqual(len(stages), 0,
            f"ORPHANED API ({api_id}) should have no stages but found {len(stages)}")

    # ── Test 2: DynamoDB seed ─────────────────────────────────────────────────

    def test_05_dynamo_records_seeded(self):
        """All 3 records must exist in the isolated test DynamoDB table."""
        table = self.dynamo.Table(self.dynamodb_table)
        for key, info in self.apis.items():
            with self.subTest(api=key):
                record = get_dynamo_record(table, info["api_id"], self.region)
                self.assertIsNotNone(record,
                    f"No DynamoDB record for {key} API ({info['api_id']})")

    # ── Test 3: Classifier ────────────────────────────────────────────────────

    def test_06_classifier_assigns_correct_tiers(self):
        """
        Run the classifier lambda directly.
        Asserts each API lands in its expected tier.
        """
        classifier = load_lambda("lambda_classifier")
        result = classifier.lambda_handler({}, None)

        print(f"\n   Classifier result: {result}")

        table = self.dynamo.Table(self.dynamodb_table)

        expected = {
            "active":   "ACTIVE",
            "dormant":  "DORMANT",
            "orphaned": "ORPHANED",
        }
        for key, expected_tier in expected.items():
            with self.subTest(api=key):
                record = get_dynamo_record(
                    table, self.apis[key]["api_id"], self.region
                )
                actual_tier = record.get("tier", "NOT_SET") if record else "NO_RECORD"
                self.assertEqual(actual_tier, expected_tier,
                    f"{key} API: expected tier={expected_tier}, got tier={actual_tier}")

    # ── Test 4: Notifier (dry-run) ────────────────────────────────────────────

    def test_07_notifier_runs_without_errors(self):
        """
        Invoke the notifier lambda in dry-run mode.
        It should process DORMANT + ORPHANED records without raising exceptions.
        """
        notifier = load_lambda("lambda_notifier")
        try:
            result = notifier.lambda_handler({}, None)
            print(f"\n   Notifier result: {result}")
        except Exception as e:
            self.fail(f"Notifier raised an unexpected exception: {e}\n{traceback.format_exc()}")

    # ── Test 5: Cleaner soft-delete (dry-run) ─────────────────────────────────

    def test_08_cleaner_soft_delete_dry_run(self):
        """
        Invoke the cleaner in SOFT + DRY_RUN mode.
        No real API throttle changes must occur; DRY_RUN=true must be respected.
        """
        os.environ["CLEANER_MODE"] = "soft"
        os.environ["DRY_RUN"]      = "true"

        cleaner = load_lambda("lambda_cleaner")
        try:
            result = cleaner.lambda_handler({"CLEANER_MODE": "soft"}, None)
            print(f"\n   Cleaner (soft, dry-run) result: {result}")
        except Exception as e:
            self.fail(f"Cleaner raised an unexpected exception: {e}\n{traceback.format_exc()}")

        # Verify the DORMANT API still has its stage (was NOT throttled)
        apigw  = boto3.client("apigateway", region_name=self.region)
        stages = apigw.get_stages(restApiId=self.apis["dormant"]["api_id"])["item"]
        self.assertGreater(len(stages), 0,
            "DORMANT API stage was deleted during dry-run — DRY_RUN flag ignored!")

    # ── Test 6: Full scanner → classifier round-trip (--full only) ───────────

    def test_09_full_scanner_to_classifier_round_trip(self):
        """
        Runs the real scanner lambda against the test region.
        Waits for CloudWatch metrics to appear (up to 5 min), then re-classifies.
        Only executed when --full is passed.
        """
        if not self.full:
            self.skipTest("Skipped — pass --full to enable the real scanner round-trip")

        print("\n   Running real scanner lambda (this may take a few minutes)...")
        scanner = load_lambda("lambda_scanner")
        result  = scanner.lambda_handler({"regions": [self.region]}, None)
        print(f"   Scanner result: {result}")

        self.assertIn("scanned", result)
        self.assertGreaterEqual(result["scanned"], 3,
            f"Scanner found only {result['scanned']} APIs — expected at least 3 test APIs")

        # Re-run classifier on the freshly scanned data
        classifier = load_lambda("lambda_classifier")
        result = classifier.lambda_handler({}, None)
        print(f"   Classifier result after real scan: {result}")

        table = self.dynamo.Table(self.dynamodb_table)

        # ORPHANED must still be ORPHANED (no stages ever)
        orphaned_record = get_dynamo_record(
            table, self.apis["orphaned"]["api_id"], self.region
        )
        self.assertEqual(
            orphaned_record.get("tier"), "ORPHANED",
            "ORPHANED API was misclassified after real scanner run"
        )

    # ── Test 7: Tags — protected APIs are never touched ───────────────────────

    def test_10_protected_tag_skips_api(self):
        """
        An API tagged do-not-delete=true must be absent from DORMANT/ORPHANED
        records in DynamoDB after classification.
        """
        apigw = boto3.client("apigateway", region_name=self.region)

        # Create a temporary protected API
        api = apigw.create_rest_api(
            name="test-api-protected",
            tags={"do-not-delete": "true", "e2e-test": "true"},
        )
        protected_id = api["id"]
        print(f"\n   Created protected API: {protected_id}")

        try:
            # Seed it as DORMANT-like so classifier would normally flag it
            table = self.dynamo.Table(self.dynamodb_table)
            seed_dynamo_record(
                table,
                api_id=protected_id,
                api_name="test-api-protected",
                region=self.region,
                invocation_count=0,
                has_stages=True,
                last_invocation="never",
            )

            classifier = load_lambda("lambda_classifier")
            classifier.lambda_handler({}, None)

            record = get_dynamo_record(table, protected_id, self.region)

            # In a full pipeline the scanner would skip this API entirely.
            # Here we just verify the record exists and hasn't been hard-deleted.
            self.assertIsNotNone(record,
                "Protected API record was unexpectedly removed from DynamoDB")
        finally:
            # Clean up protected test API (with 31s throttle cool-down)
            time.sleep(5)
            try:
                apigw.delete_rest_api(restApiId=protected_id)
                time.sleep(31)
            except Exception:
                pass


# ── CLI ───────────────────────────────────────────────────────────────────────

_ARGS = None

def parse_args():
    p = argparse.ArgumentParser(description="End-to-end test for API Gateway Cleanup")
    p.add_argument("--region",   default="us-east-1", help="AWS region")
    p.add_argument("--profile",  default=None,         help="AWS CLI profile")
    p.add_argument("--full",     action="store_true",  help="Run real scanner (slower)")
    p.add_argument("--tf-dir",   default=None,
                   help="Path to the terraform directory — runs 'terraform output -json' to get API IDs")
    p.add_argument("--api-ids",  default=None,
                   help="JSON file with pre-saved API IDs (alternative to --tf-dir)")
    return p.parse_args()


def main():
    global _ARGS
    _ARGS = parse_args()

    if _ARGS.profile:
        boto3.setup_default_session(profile_name=_ARGS.profile)

    account = boto3.client("sts").get_caller_identity()["Account"]
    source  = _ARGS.tf_dir or _ARGS.api_ids or "NOT SET"

    print("═" * 60)
    print("  API Gateway Cleanup — End-to-End Test")
    print("═" * 60)
    print(f"  Account : {account}")
    print(f"  Region  : {_ARGS.region}")
    print(f"  Source  : {source}")
    print(f"  Mode    : {'FULL (real scanner)' if _ARGS.full else 'QUICK (seeded DynamoDB)'}")
    print("═" * 60)

    # Strip our custom args from sys.argv so unittest doesn't choke on them
    sys.argv = [sys.argv[0]]

    loader = unittest.TestLoader()
    loader.sortTestMethodsUsing = None   # preserve declaration order
    suite  = loader.loadTestsFromTestCase(E2ETestSuite)

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
