# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "cluster_name" {
  value = module.eks.cluster_name
}
output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}
output "cluster_oidc_provider_arn" {
  value = module.eks.oidc_provider_arn
}

output "gateway_proxy_role_arn" {
  value = module.irsa.gateway_proxy_role_arn
}
output "admin_api_role_arn" {
  value = module.irsa.admin_api_role_arn
}
output "all_role_arns" {
  value = module.irsa.all_role_arns
}

output "aurora_endpoint" {
  value = module.aurora.cluster_endpoint
}
output "aurora_master_user_secret_arn" {
  value     = module.aurora.master_user_secret_arn
  sensitive = true
}

# Terraform-managed DB secrets (enable_rds_proxy = true 시)
output "gateway_user_secret_arn" {
  value     = module.aurora.gateway_user_secret_arn
  sensitive = true
}
output "db_secret_arn" {
  value     = module.aurora.db_secret_arn
  sensitive = true
}

# RDS Proxy (enable_rds_proxy = true 시에만 값 존재)
output "rds_proxy_enabled" {
  value = module.aurora.proxy_enabled
}
output "rds_proxy_endpoint" {
  value = module.aurora.proxy_endpoint
}
# Helm values database.external.host 에 주입할 호스트 — Proxy on/off 에 관계없이 올바른 값.
output "application_db_endpoint" {
  value = module.aurora.application_endpoint
}

output "elasticache_endpoint" {
  value = module.elasticache.primary_endpoint_address
}
output "elasticache_auth_token_secret_arn" {
  value     = module.elasticache.auth_token_secret_arn
  sensitive = true
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "nat_public_ips" {
  value = module.vpc.nat_public_ips
}

output "kubectl_config_command" {
  value = "aws eks update-kubeconfig --region ${var.aws_region} --name ${module.eks.cluster_name}"
}

# ─── Cognito (OIDC IDP) ───
output "cognito_user_pool_id" {
  value = module.cognito.user_pool_id
}
output "cognito_client_id" {
  value = module.cognito.client_id
}
output "cognito_issuer_url" {
  value       = module.cognito.issuer_url
  description = "admin-api 의 OIDC_ISSUER_URL 값"
}
output "cognito_hosted_ui_domain" {
  value       = module.cognito.hosted_ui_domain
  description = "Cognito Hosted UI 도메인 (사용자 로그인 페이지 호스트)"
}
output "cognito_groups" {
  value       = module.cognito.groups
  description = "Cognito user groups — admin 이 이 그룹에 사용자를 추가"
}

# ─── admin-chat-agent (enable_chat_agent=true 일 때만) ───
output "chat_agent_ecr_url" {
  value       = try(module.agentcore_runtime[0].ecr_repository_url, null)
  description = "admin-chat-agent 컨테이너 image 를 push 할 ECR URL"
}
output "chat_agent_execution_role_arn" {
  value       = try(module.agentcore_runtime[0].agent_execution_role_arn, null)
  description = "AgentCore CreateAgentRuntime 호출 시 전달할 execution role ARN"
}
output "chat_agent_staging_bucket" {
  value       = try(module.agentcore_runtime[0].staging_bucket_name, null)
  description = "SQL → Code Specialist 데이터 전달용 S3 staging bucket"
}
output "chat_agent_name" {
  value       = try(module.agentcore_runtime[0].agent_name, null)
  description = "AgentCore Runtime 등록 시 사용할 agent name"
}
