# Vended USAGE_LOGS destination for AgentCore Runtime session-level usage.
#
# AgentCore Runtime can deliver per-session resource usage (vCPU-hours and
# GB-hours at one-second granularity, keyed by session id) as a CloudWatch Logs
# *vended log*. Vended delivery is three objects: a delivery **source** naming
# the runtime and the log type, a delivery **destination** naming where the
# rows go, and a **delivery** joining the two. Only the destination is stack
# infrastructure — one log group and one destination for the whole stack — so
# only it lives here. Sources and deliveries are per runtime, runtimes are
# created outside terraform (`agentcore launch`, CreateHarness), and the server
# creates those two idempotently (`observability_service.ensure_usage_logs`).
#
# The log group name follows the vended-logs convention (`/aws/vendedlogs/...`);
# CloudWatch permits vended delivery into a same-account group without a
# destination policy. Retention is bounded because the collector folds every
# row into the usage table within minutes and the rows are only ever re-read
# for a backfill.

resource "aws_cloudwatch_log_group" "usage" {
  name              = "/aws/vendedlogs/bedrock-agentcore/runtime/USAGE_LOGS/${var.project}"
  retention_in_days = var.retention_in_days
}

resource "aws_cloudwatch_log_delivery_destination" "usage" {
  name          = "${var.project}-runtime-usage-logs"
  output_format = "json"

  delivery_destination_configuration {
    destination_resource_arn = aws_cloudwatch_log_group.usage.arn
  }
}

# CloudWatch Logs delivers vended logs as the `delivery.logs.amazonaws.com`
# principal, and CreateDelivery refuses a destination whose log group does not
# let that principal write ("Access Denied for this Delivery Destination",
# measured 2026-09-23). The console adds this policy silently; the API does not.
data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "delivery_write" {
  statement {
    sid     = "AWSLogDeliveryWrite"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    resources = ["${aws_cloudwatch_log_group.usage.arn}:log-stream:*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "delivery_write" {
  policy_name     = "${var.project}-runtime-usage-logs-delivery"
  policy_document = data.aws_iam_policy_document.delivery_write.json
}
