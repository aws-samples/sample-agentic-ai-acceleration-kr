# Alarms for the layers terraform can name.
#
# Per-agent alarms are deliberately absent here. The runtimes they would watch
# are created by `harness_service.create_harness` at request time, so terraform
# never learns their ids, and CloudWatch will not populate a partial dimension
# set — a hand-written dimension list produces an alarm stuck in
# INSUFFICIENT_DATA that reads as "nothing is wrong". Those alarms come from
# `server/scripts/sync_agent_alarms.py`, which reuses the metric discovery the
# insights dashboard already does.

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-insights-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# The platform's own front door. A 5xx here is our bug, not an agent's.
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${var.project}-alb-5xx"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  # Missing data is not a failure: a quiet five minutes has no datapoints at all.
  treat_missing_data = "notBreaching"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.target_group_arn_suffix
  }
  alarm_description = "Server returned 5xx to the ALB. Check /ecs/${var.project}/server."
  alarm_actions     = [aws_sns_topic.alerts.arn]
  ok_actions        = [aws_sns_topic.alerts.arn]
}

# A tier that keeps dying stops answering health checks. This catches the crash
# loop that a 5xx alarm misses, because a task that never starts serves nothing.
#
# **Not `ECS/ContainerInsights:RunningTaskCount`.** Container Insights is
# `disabled` on this cluster (measured 2026-08-16 via `describe-clusters
# --include SETTINGS`), so that metric is never published — and an alarm on an
# unpublished metric with `treat_missing_data = "breaching"` sits in ALARM
# forever and pages continuously about nothing. Enabling Container Insights
# would fix the metric and add per-metric charges to a shared account; the ALB
# already publishes a health signal for free.
#
# `HealthyHostCount` on the web target group is that signal, and it is published
# continuously — 72 datapoints in six hours, measured, so `breaching` is
# defensible here in a way it was not above. The dimension pair is required:
# `{LoadBalancer, TargetGroup}` returned 72 datapoints while `{TargetGroup}`
# alone returned zero.
#
# This watches the *web* tier directly. The server tier is covered by the 5xx
# alarm above, because web proxying to a dead server returns 5xx through this
# same target group — the server is reached by service discovery, not by the ALB,
# so it has no target group of its own to watch.
resource "aws_cloudwatch_metric_alarm" "web_healthy_hosts" {
  alarm_name          = "${var.project}-web-healthy-hosts"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  dimensions = {
    LoadBalancer = var.alb_arn_suffix
    TargetGroup  = var.target_group_arn_suffix
  }
  alarm_description = "No healthy web target behind the ALB. Likely a crash loop; check /ecs/${var.project}/web and /ecs/${var.project}/server."
  alarm_actions     = [aws_sns_topic.alerts.arn]
  ok_actions        = [aws_sns_topic.alerts.arn]
}

# Usage writes are ADD-only counters on the hot path of every turn. A throttle
# here silently loses attribution, and nothing in the UI would show it.
#
# `WriteThrottleEvents`, not `ThrottledRequests`: the latter is dimensioned
# `{TableName, Operation}` and would never match the `{TableName}`-only set
# below, giving an alarm that can never fire. Neither metric appears in
# `list-metrics` for this table yet, because it has never been throttled — that
# is what `notBreaching` is for, and it is why the metric name had to be checked
# against the documented dimension sets rather than against what is published.
resource "aws_cloudwatch_metric_alarm" "usage_throttles" {
  alarm_name          = "${var.project}-usage-throttles"
  namespace           = "AWS/DynamoDB"
  metric_name         = "WriteThrottleEvents"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    TableName = var.usage_table_name
  }
  alarm_description = "Usage counter writes are being throttled; turns may go unattributed."
  alarm_actions     = [aws_sns_topic.alerts.arn]
  ok_actions        = [aws_sns_topic.alerts.arn]
}
