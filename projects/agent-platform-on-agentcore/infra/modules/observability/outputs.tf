output "log_group_name" {
  description = "Log group the server queries for per-session usage (USAGE_LOG_GROUP)."
  value       = aws_cloudwatch_log_group.usage.name
}

output "destination_arn" {
  description = "Delivery destination the server attaches each runtime's USAGE_LOGS source to (USAGE_LOG_DESTINATION_ARN)."
  value       = aws_cloudwatch_log_delivery_destination.usage.arn
}
