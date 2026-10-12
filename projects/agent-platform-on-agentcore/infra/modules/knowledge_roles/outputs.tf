output "kb_service_role_arn" {
  description = "Passed as CreateKnowledgeBase roleArn."
  value       = aws_iam_role.kb_service.arn
}

output "kb_gateway_role_arn" {
  description = "Passed as CreateGateway roleArn for knowledge-base gateways."
  value       = aws_iam_role.kb_gateway.arn
}
