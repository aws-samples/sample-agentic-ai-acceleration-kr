# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "primary_endpoint_address" {
  description = "Writer endpoint (non-cluster mode)"
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "configuration_endpoint_address" {
  description = "Cluster mode configuration endpoint (prod)"
  value       = aws_elasticache_replication_group.this.configuration_endpoint_address
}

output "reader_endpoint_address" {
  value = aws_elasticache_replication_group.this.reader_endpoint_address
}

output "port" {
  value = aws_elasticache_replication_group.this.port
}

output "auth_token_secret_arn" {
  description = "AUTH 토큰이 저장된 Secrets Manager ARN"
  value       = aws_secretsmanager_secret.auth_token.arn
}

output "auth_token_secret_name" {
  description = "AUTH 토큰 Secrets Manager name (ESO에서 참조)"
  value       = aws_secretsmanager_secret.auth_token.name
}

output "security_group_id" {
  value = aws_security_group.this.id
}
