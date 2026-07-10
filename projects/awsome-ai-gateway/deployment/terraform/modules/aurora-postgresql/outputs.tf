# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "cluster_identifier" {
  value = module.aurora.cluster_id
}

output "cluster_endpoint" {
  description = "Writer endpoint (애플리케이션이 쓰기용으로 연결)"
  value       = module.aurora.cluster_endpoint
}

output "cluster_reader_endpoint" {
  description = "Reader endpoint (read-replica 조회용)"
  value       = module.aurora.cluster_reader_endpoint
}

output "cluster_port" {
  value = module.aurora.cluster_port
}

output "database_name" {
  value = module.aurora.cluster_database_name
}

output "master_username" {
  value = module.aurora.cluster_master_username
}

output "master_user_secret_arn" {
  description = "Secrets Manager에 저장된 마스터 유저 비밀번호 ARN"
  value       = module.aurora.cluster_master_user_secret[0].secret_arn
}

output "security_group_id" {
  value = module.aurora.security_group_id
}

# ------------------------------------------------------------------------------
# Terraform-managed DB Secrets (enable_rds_proxy = true 시에만 생성)
# ------------------------------------------------------------------------------
output "gateway_user_secret_arn" {
  description = "application gateway user credentials (Proxy auth 용 format: {username, password})"
  value       = var.enable_rds_proxy ? aws_secretsmanager_secret.gateway_user[0].arn : null
}

output "db_secret_arn" {
  description = "Helm ExternalSecret 이 참조하는 /db secret ARN (format: {password, master_password})"
  value       = var.enable_rds_proxy ? aws_secretsmanager_secret.db[0].arn : null
}

# ------------------------------------------------------------------------------
# RDS Proxy outputs (enable_rds_proxy = false 면 모두 null)
# ------------------------------------------------------------------------------
output "proxy_enabled" {
  value = var.enable_rds_proxy
}

output "proxy_endpoint" {
  description = "RDS Proxy writer endpoint. Aurora cluster_endpoint 대신 애플리케이션이 연결할 호스트."
  value       = var.enable_rds_proxy ? aws_db_proxy.this[0].endpoint : null
}

output "proxy_arn" {
  value = var.enable_rds_proxy ? aws_db_proxy.this[0].arn : null
}

output "proxy_security_group_id" {
  value = var.enable_rds_proxy ? aws_security_group.proxy[0].id : null
}

# 애플리케이션이 실제로 연결해야 할 호스트 — Proxy 활성화면 Proxy, 아니면 Aurora writer
output "application_endpoint" {
  description = "애플리케이션이 쓸 호스트. Proxy 활성화 시 Proxy endpoint, 아니면 Aurora writer endpoint."
  value       = var.enable_rds_proxy ? aws_db_proxy.this[0].endpoint : module.aurora.cluster_endpoint
}
