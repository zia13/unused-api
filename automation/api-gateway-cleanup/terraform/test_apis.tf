####################################################################
#  Test APIs — 3 REST APIs for end-to-end testing
#
#  API 1: test-api-active   → has a stage + deployment  (expected: ACTIVE)
#  API 2: test-api-dormant  → has a stage + deployment  (expected: DORMANT)
#  API 3: test-api-orphaned → NO stage, NO deployment   (expected: ORPHANED)
#
#  Toggle with: create_test_apis = true  in terraform.tfvars
#  Default:     false  (no test resources in prod unless explicitly enabled)
####################################################################

variable "create_test_apis" {
  description = "Set to true to provision the 3 e2e test APIs"
  type        = bool
  default     = false
}

# ── API 1: test-api-active ────────────────────────────────────────
# Has a stage + deployment. The e2e test seeds 9000 invocations in
# DynamoDB, which classifies it as ACTIVE.

resource "aws_api_gateway_rest_api" "test_active" {
  count       = var.create_test_apis ? 1 : 0
  name        = "test-api-active"
  description = "E2E test — ACTIVE tier (has stage, seeded with traffic)"

  tags = {
    Environment  = var.environment
    e2e-test     = "true"
    expected-tier = "ACTIVE"
    ManagedBy    = "terraform"
  }
}

resource "aws_api_gateway_method" "test_active_get" {
  count         = var.create_test_apis ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.test_active[0].id
  resource_id   = aws_api_gateway_rest_api.test_active[0].root_resource_id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "test_active_mock" {
  count       = var.create_test_apis ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.test_active[0].id
  resource_id = aws_api_gateway_rest_api.test_active[0].root_resource_id
  http_method = aws_api_gateway_method.test_active_get[0].http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "test_active_200" {
  count       = var.create_test_apis ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.test_active[0].id
  resource_id = aws_api_gateway_rest_api.test_active[0].root_resource_id
  http_method = aws_api_gateway_method.test_active_get[0].http_method
  status_code = "200"
}

resource "aws_api_gateway_integration_response" "test_active_mock_resp" {
  count       = var.create_test_apis ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.test_active[0].id
  resource_id = aws_api_gateway_rest_api.test_active[0].root_resource_id
  http_method = aws_api_gateway_method.test_active_get[0].http_method
  status_code = aws_api_gateway_method_response.test_active_200[0].status_code
  response_templates = {
    "application/json" = "{\"message\": \"ok\"}"
  }
  depends_on = [aws_api_gateway_integration.test_active_mock]
}

resource "aws_api_gateway_deployment" "test_active" {
  count       = var.create_test_apis ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.test_active[0].id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_method.test_active_get[0].id,
      aws_api_gateway_integration.test_active_mock[0].id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [aws_api_gateway_integration_response.test_active_mock_resp]
}

resource "aws_api_gateway_stage" "test_active" {
  count         = var.create_test_apis ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.test_active[0].id
  deployment_id = aws_api_gateway_deployment.test_active[0].id
  stage_name    = "test"
  description   = "E2E test stage"

  tags = {
    e2e-test  = "true"
    ManagedBy = "terraform"
  }
}

# ── API 2: test-api-dormant ───────────────────────────────────────
# Has a stage + deployment but the e2e test seeds 0 invocations,
# so the classifier marks it DORMANT.

resource "aws_api_gateway_rest_api" "test_dormant" {
  count       = var.create_test_apis ? 1 : 0
  name        = "test-api-dormant"
  description = "E2E test — DORMANT tier (has stage, zero traffic)"

  tags = {
    Environment   = var.environment
    e2e-test      = "true"
    expected-tier = "DORMANT"
    ManagedBy     = "terraform"
  }
}

resource "aws_api_gateway_method" "test_dormant_get" {
  count         = var.create_test_apis ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.test_dormant[0].id
  resource_id   = aws_api_gateway_rest_api.test_dormant[0].root_resource_id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "test_dormant_mock" {
  count       = var.create_test_apis ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.test_dormant[0].id
  resource_id = aws_api_gateway_rest_api.test_dormant[0].root_resource_id
  http_method = aws_api_gateway_method.test_dormant_get[0].http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "test_dormant_200" {
  count       = var.create_test_apis ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.test_dormant[0].id
  resource_id = aws_api_gateway_rest_api.test_dormant[0].root_resource_id
  http_method = aws_api_gateway_method.test_dormant_get[0].http_method
  status_code = "200"
}

resource "aws_api_gateway_integration_response" "test_dormant_mock_resp" {
  count       = var.create_test_apis ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.test_dormant[0].id
  resource_id = aws_api_gateway_rest_api.test_dormant[0].root_resource_id
  http_method = aws_api_gateway_method.test_dormant_get[0].http_method
  status_code = aws_api_gateway_method_response.test_dormant_200[0].status_code
  response_templates = {
    "application/json" = "{\"message\": \"ok\"}"
  }
  depends_on = [aws_api_gateway_integration.test_dormant_mock]
}

resource "aws_api_gateway_deployment" "test_dormant" {
  count       = var.create_test_apis ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.test_dormant[0].id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_method.test_dormant_get[0].id,
      aws_api_gateway_integration.test_dormant_mock[0].id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [aws_api_gateway_integration_response.test_dormant_mock_resp]
}

resource "aws_api_gateway_stage" "test_dormant" {
  count         = var.create_test_apis ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.test_dormant[0].id
  deployment_id = aws_api_gateway_deployment.test_dormant[0].id
  stage_name    = "test"
  description   = "E2E test stage"

  tags = {
    e2e-test  = "true"
    ManagedBy = "terraform"
  }
}

# ── API 3: test-api-orphaned ──────────────────────────────────────
# Has NO deployment and NO stage. The scanner sets has_stages=false,
# so the classifier marks it ORPHANED.

resource "aws_api_gateway_rest_api" "test_orphaned" {
  count       = var.create_test_apis ? 1 : 0
  name        = "test-api-orphaned"
  description = "E2E test — ORPHANED tier (no stage, no deployment)"

  tags = {
    Environment   = var.environment
    e2e-test      = "true"
    expected-tier = "ORPHANED"
    ManagedBy     = "terraform"
  }
}

resource "aws_api_gateway_method" "test_orphaned_get" {
  count         = var.create_test_apis ? 1 : 0
  rest_api_id   = aws_api_gateway_rest_api.test_orphaned[0].id
  resource_id   = aws_api_gateway_rest_api.test_orphaned[0].root_resource_id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "test_orphaned_mock" {
  count       = var.create_test_apis ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.test_orphaned[0].id
  resource_id = aws_api_gateway_rest_api.test_orphaned[0].root_resource_id
  http_method = aws_api_gateway_method.test_orphaned_get[0].http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

# ── Outputs ───────────────────────────────────────────────────────

output "test_api_active_id" {
  description = "REST API ID of test-api-active"
  value       = var.create_test_apis ? aws_api_gateway_rest_api.test_active[0].id : ""
}

output "test_api_active_invoke_url" {
  description = "Invoke URL for test-api-active"
  value       = var.create_test_apis ? aws_api_gateway_stage.test_active[0].invoke_url : ""
}

output "test_api_dormant_id" {
  description = "REST API ID of test-api-dormant"
  value       = var.create_test_apis ? aws_api_gateway_rest_api.test_dormant[0].id : ""
}

output "test_api_dormant_invoke_url" {
  description = "Invoke URL for test-api-dormant"
  value       = var.create_test_apis ? aws_api_gateway_stage.test_dormant[0].invoke_url : ""
}

output "test_api_orphaned_id" {
  description = "REST API ID of test-api-orphaned (no stage, no invoke URL)"
  value       = var.create_test_apis ? aws_api_gateway_rest_api.test_orphaned[0].id : ""
}
