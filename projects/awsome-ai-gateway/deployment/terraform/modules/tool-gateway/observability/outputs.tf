output "log_group_name" {
  value       = aws_cloudwatch_log_group.gateway_logs.name
  description = "CloudWatch Log Group name for vended logs"
}

output "log_group_arn" {
  value       = aws_cloudwatch_log_group.gateway_logs.arn
  description = "CloudWatch Log Group ARN"
}

output "traces_log_group_name" {
  value       = aws_cloudwatch_log_group.gateway_traces.name
  description = "CloudWatch Log Group name for OTEL traces"
}

output "trace_sampling_percentage" {
  value       = var.manage_trace_indexing_rule ? var.trace_sampling_percentage : null
  description = "Effective X-Ray Transaction Search indexing sampling percentage (null if not managed here)"
}
