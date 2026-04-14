####################################################################
#  AWS API Gateway Cleanup — Terraform Infrastructure
#  Provisions: DynamoDB table, S3 archive bucket, IAM roles,
#              Lambda functions, EventBridge rules, SNS topic,
#              Step Functions state machine
####################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# ── Variables ─────────────────────────────────────────────────────

variable "aws_region" {
  default = "us-east-1"
}

variable "environment" {
  default = "prod"
}

variable "ses_sender_email" {
  description = "Verified SES sender email address"
}

variable "sns_alert_email" {
  description = "Email address for SNS orphan alerts"
}

variable "s3_archive_bucket_name" {
  default = "api-gateway-cleanup-archive"
}

variable "dynamodb_table_name" {
  default = "api-gateway-inventory"
}

variable "lookback_days" {
  default = "90"
}

variable "low_traffic_threshold" {
  default = "10"
}

variable "dry_run" {
  default = "true"
  description = "Set to 'false' to enable real deletions"
}

variable "soft_delete_window_days" {
  default = "7"
}

variable "notice_period_days" {
  default = "30"
}

# ── Provider ──────────────────────────────────────────────────────

provider "aws" {
  region = var.aws_region
}

locals {
  prefix = "api-gw-cleanup"
  common_env = {
    DYNAMODB_TABLE         = var.dynamodb_table_name
    S3_ARCHIVE_BUCKET      = var.s3_archive_bucket_name
    SNS_TOPIC_ARN          = aws_sns_topic.alerts.arn
    SES_SENDER_EMAIL       = var.ses_sender_email
    LOOKBACK_DAYS          = var.lookback_days
    LOW_TRAFFIC_THRESHOLD  = var.low_traffic_threshold
    DRY_RUN                = var.dry_run
    SOFT_DELETE_WINDOW_DAYS = var.soft_delete_window_days
    NOTICE_PERIOD_DAYS     = var.notice_period_days
  }
}

# ── DynamoDB Table ────────────────────────────────────────────────

resource "aws_dynamodb_table" "inventory" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "api_id"
  range_key    = "region"

  attribute {
    name = "api_id"
    type = "S"
  }

  attribute {
    name = "region"
    type = "S"
  }

  attribute {
    name = "tier"
    type = "S"
  }

  global_secondary_index {
    name            = "tier-index"
    hash_key        = "tier"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name        = var.dynamodb_table_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# ── S3 Archive Bucket ─────────────────────────────────────────────

resource "aws_s3_bucket" "archive" {
  bucket = var.s3_archive_bucket_name

  tags = {
    Name        = var.s3_archive_bucket_name
    Environment = var.environment
    Purpose     = "api-gateway-cleanup-archive"
    ManagedBy   = "terraform"
  }
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket                  = aws_s3_bucket.archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    id     = "transition-to-glacier"
    status = "Enabled"
    transition {
      days          = 90
      storage_class = "GLACIER"
    }
    expiration {
      days = 2555  # 7 years retention
    }
  }
}

# ── SNS Topic ─────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "${local.prefix}-orphan-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.sns_alert_email
}

# ── IAM Role for Lambda ───────────────────────────────────────────

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.prefix}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_policy" {
  statement {
    effect = "Allow"
    actions = [
      "apigateway:GET", "apigateway:DELETE", "apigateway:PATCH",
      "apigatewayv2:GET", "apigatewayv2:DELETE", "apigatewayv2:PATCH",
      "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics",
      "cloudwatch:PutMetricAlarm",
      "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
      "logs:DescribeLogGroups", "logs:StartQuery", "logs:GetQueryResults",
      "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem",
      "dynamodb:Scan", "dynamodb:BatchWriteItem",
      "s3:PutObject", "s3:GetObject",
      "sns:Publish",
      "ses:SendEmail",
      "sts:GetCallerIdentity",
      "account:ListRegions",
      "organizations:ListAccounts",
      "tag:GetResources",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.prefix}-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_policy.json
}

# ── Lambda: Scanner ───────────────────────────────────────────────

data "archive_file" "scanner" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/lambda_scanner.py"
  output_path = "${path.module}/builds/lambda_scanner.zip"
}

resource "aws_lambda_function" "scanner" {
  function_name    = "${local.prefix}-scanner"
  filename         = data.archive_file.scanner.output_path
  source_code_hash = data.archive_file.scanner.output_base64sha256
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_scanner.lambda_handler"
  runtime          = "python3.12"
  timeout          = 900
  memory_size      = 256

  environment {
    variables = local.common_env
  }

  tags = { ManagedBy = "terraform", Environment = var.environment }
}

# ── Lambda: Classifier ────────────────────────────────────────────

data "archive_file" "classifier" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/lambda_classifier.py"
  output_path = "${path.module}/builds/lambda_classifier.zip"
}

resource "aws_lambda_function" "classifier" {
  function_name    = "${local.prefix}-classifier"
  filename         = data.archive_file.classifier.output_path
  source_code_hash = data.archive_file.classifier.output_base64sha256
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_classifier.lambda_handler"
  runtime          = "python3.12"
  timeout          = 300
  memory_size      = 128

  environment {
    variables = local.common_env
  }

  tags = { ManagedBy = "terraform", Environment = var.environment }
}

# ── Lambda: Notifier ──────────────────────────────────────────────

data "archive_file" "notifier" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/lambda_notifier.py"
  output_path = "${path.module}/builds/lambda_notifier.zip"
}

resource "aws_lambda_function" "notifier" {
  function_name    = "${local.prefix}-notifier"
  filename         = data.archive_file.notifier.output_path
  source_code_hash = data.archive_file.notifier.output_base64sha256
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_notifier.lambda_handler"
  runtime          = "python3.12"
  timeout          = 300
  memory_size      = 128

  environment {
    variables = local.common_env
  }

  tags = { ManagedBy = "terraform", Environment = var.environment }
}

# ── Lambda: Cleaner ───────────────────────────────────────────────

data "archive_file" "cleaner" {
  type        = "zip"
  source_file = "${path.module}/../lambdas/lambda_cleaner.py"
  output_path = "${path.module}/builds/lambda_cleaner.zip"
}

resource "aws_lambda_function" "cleaner" {
  function_name    = "${local.prefix}-cleaner"
  filename         = data.archive_file.cleaner.output_path
  source_code_hash = data.archive_file.cleaner.output_base64sha256
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_cleaner.lambda_handler"
  runtime          = "python3.12"
  timeout          = 900
  memory_size      = 256

  environment {
    variables = merge(local.common_env, { CLEANER_MODE = "soft" })
  }

  tags = { ManagedBy = "terraform", Environment = var.environment }
}

# ── Step Functions State Machine ──────────────────────────────────

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${local.prefix}-sfn-role"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

resource "aws_iam_role_policy" "sfn" {
  name = "${local.prefix}-sfn-policy"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [
        aws_lambda_function.scanner.arn,
        aws_lambda_function.classifier.arn,
        aws_lambda_function.notifier.arn,
        aws_lambda_function.cleaner.arn,
      ]
    }]
  })
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "${local.prefix}-pipeline"
  role_arn = aws_iam_role.sfn.arn

  definition = jsonencode({
    Comment = "API Gateway unused API cleanup pipeline"
    StartAt = "Scan"
    States = {
      Scan = {
        Type     = "Task"
        Resource = aws_lambda_function.scanner.arn
        Next     = "Classify"
      }
      Classify = {
        Type     = "Task"
        Resource = aws_lambda_function.classifier.arn
        Next     = "Notify"
      }
      Notify = {
        Type     = "Task"
        Resource = aws_lambda_function.notifier.arn
        Next     = "WaitForSoftDeleteWindow"
      }
      WaitForSoftDeleteWindow = {
        Type    = "Wait"
        Seconds = tonumber(var.soft_delete_window_days) * 86400
        Next    = "SoftClean"
      }
      SoftClean = {
        Type     = "Task"
        Resource = aws_lambda_function.cleaner.arn
        Parameters = { CLEANER_MODE = "soft" }
        Next     = "WaitForHardDeleteWindow"
      }
      WaitForHardDeleteWindow = {
        Type    = "Wait"
        Seconds = 604800  # 7 days
        Next    = "HardClean"
      }
      HardClean = {
        Type     = "Task"
        Resource = aws_lambda_function.cleaner.arn
        Parameters = { CLEANER_MODE = "hard" }
        End      = true
      }
    }
  })

  tags = { ManagedBy = "terraform", Environment = var.environment }
}

# ── EventBridge: Weekly trigger ───────────────────────────────────

resource "aws_scheduler_schedule" "weekly_scan" {
  name = "${local.prefix}-weekly"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "cron(0 8 ? * MON *)"

  target {
    arn      = aws_sfn_state_machine.pipeline.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ triggered_by = "scheduler" })
  }
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.prefix}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${local.prefix}-scheduler-policy"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = aws_sfn_state_machine.pipeline.arn
    }]
  })
}

# ── Outputs ───────────────────────────────────────────────────────

output "dynamodb_table_name" {
  value = aws_dynamodb_table.inventory.name
}

output "s3_archive_bucket" {
  value = aws_s3_bucket.archive.bucket
}

output "sns_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.pipeline.arn
}

output "scanner_lambda_arn" {
  value = aws_lambda_function.scanner.arn
}
