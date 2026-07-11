output "gateway_id" {
  value       = aws_bedrockagentcore_gateway.this.gateway_id
  description = "AgentCore Gateway ID"
}

output "gateway_arn" {
  value       = aws_bedrockagentcore_gateway.this.gateway_arn
  description = "AgentCore Gateway ARN"
}

output "gateway_url" {
  value       = aws_bedrockagentcore_gateway.this.gateway_url
  description = "AgentCore Gateway HTTPS endpoint URL"
}

output "gateway_role_arn" {
  value       = aws_iam_role.gateway.arn
  description = "IAM role ARN for the gateway"
}
