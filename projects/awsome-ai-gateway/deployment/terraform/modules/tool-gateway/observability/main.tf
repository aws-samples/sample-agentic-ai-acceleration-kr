data "aws_caller_identity" "current" {}

# ============================================================
# CloudWatch Log Groups for AgentCore Gateway Vended Logs
# ============================================================
# Vended logs from AgentCore are delivered here with transaction data:
# - requestBody, responseBody
# - trace_id, span_id
# - Supports CloudWatch Logs Insights queries for debugging

resource "aws_cloudwatch_log_group" "gateway_logs" {
  name              = "/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/${var.gateway_id}"
  retention_in_days = 30
  log_group_class   = "STANDARD" # Required for CloudWatch Logs Insights (Transaction Search)

  tags = {
    Component = "observability"
    Resource  = var.gateway_id
  }
}

resource "aws_cloudwatch_log_group" "gateway_traces" {
  name              = "/aws/vendedlogs/bedrock-agentcore/gateway/TRACES/${var.gateway_id}"
  retention_in_days = 30
  log_group_class   = "STANDARD"

  tags = {
    Component = "observability"
    Resource  = var.gateway_id
  }
}

# ============================================================
# Vended Logs Delivery (declarative)
# ============================================================
# AgentCore is a CloudWatch "vended logs" producer. Delivery is wired with the
# standard CloudWatch Logs delivery primitives:
#   delivery-source (the gateway) -> delivery-destination (a log group) -> delivery
# This replaces the previous null_resource/local-exec approach, which called a
# non-existent `update-gateway --vended-logs-configuration` flag and silently
# no-op'd (on_failure = continue). These resources are idempotent and visible
# in state.

# --- APPLICATION_LOGS ---

resource "aws_cloudwatch_log_delivery_source" "application_logs" {
  name         = "${var.project_name}-${var.environment}-gateway-logs"
  log_type     = "APPLICATION_LOGS"
  resource_arn = var.gateway_arn

  tags = {
    Component = "observability"
  }
}

resource "aws_cloudwatch_log_delivery_destination" "application_logs" {
  name          = "${var.project_name}-${var.environment}-gateway-logs-dest"
  output_format = "json"

  delivery_destination_configuration {
    destination_resource_arn = aws_cloudwatch_log_group.gateway_logs.arn
  }

  tags = {
    Component = "observability"
  }
}

resource "aws_cloudwatch_log_delivery" "application_logs" {
  delivery_source_name     = aws_cloudwatch_log_delivery_source.application_logs.name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.application_logs.arn

  tags = {
    Component = "observability"
  }
}

# --- TRACES (OTEL vended spans) ---

resource "aws_cloudwatch_log_delivery_source" "traces" {
  name         = "${var.project_name}-${var.environment}-gateway-traces"
  log_type     = "TRACES"
  resource_arn = var.gateway_arn

  tags = {
    Component = "observability"
  }
}

# TRACES are delivered to X-Ray (Transaction Search), not a CloudWatch log group.
# An XRAY destination takes no destination_resource_arn.
resource "aws_cloudwatch_log_delivery_destination" "traces" {
  name                      = "${var.project_name}-${var.environment}-gateway-traces-dest"
  delivery_destination_type = "XRAY"

  tags = {
    Component = "observability"
  }
}

resource "aws_cloudwatch_log_delivery" "traces" {
  delivery_source_name     = aws_cloudwatch_log_delivery_source.traces.name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.traces.arn

  tags = {
    Component = "observability"
  }
}

# ============================================================
# X-Ray Transaction Search — indexing sampling rate
# ============================================================
# AgentCore delivers gateway OTEL spans to X-Ray Transaction Search (the TRACES
# delivery above, destination type XRAY). How many of those spans get INDEXED
# for querying is governed by the account/region-global "Default" indexing rule,
# NOT by the delivery. Its out-of-the-box value is 1%, which makes low-traffic
# dev gateways look empty in the traces dashboard ("no traces in range") even
# though every request is logged. We pin it explicitly per-environment.
#
# Caveats (see the variable docs):
#   - "Default" is a singleton, account+region-global. Only one stack may own it.
#   - It affects ALL trace-producing resources in the region, not just this gw.
resource "aws_xray_indexing_rule" "default" {
  count = var.manage_trace_indexing_rule ? 1 : 0

  name = "Default"

  rule {
    probabilistic {
      desired_sampling_percentage = var.trace_sampling_percentage
    }
  }
}
