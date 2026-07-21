# ============================================================
# Terraform Output Interface (consumed by tools/scripts)
# ============================================================

output "project_name" {
  value       = var.project_name
  description = "Project identifier"
}

output "region" {
  value       = var.aws_region
  description = "AWS region"
}

output "gateway_id" {
  value       = module.gateway.gateway_id
  description = "AgentCore Gateway ID"
}

output "gateway_url" {
  value       = module.gateway.gateway_url
  description = "AgentCore Gateway HTTPS endpoint"
}

output "gateway_arn" {
  value       = module.gateway.gateway_arn
  description = "AgentCore Gateway ARN"
}

output "cognito_user_pool_id" {
  value       = module.auth.user_pool_id
  description = "Cognito User Pool ID"
}

output "cognito_domain" {
  value       = module.auth.domain
  description = "Cognito domain for auth endpoints"
}

output "cognito_client_id" {
  value       = module.auth.web_client_id
  description = "Cognito Web Client ID"
}

output "cognito_resource_server_id" {
  value       = module.auth.resource_server_id
  description = "Cognito Resource Server ID"
}

output "identity_provider_arns" {
  value       = local.filtered_identity_providers
  description = "Map of engine name -> credential provider ARN"
  sensitive   = false
}

output "lambda_function_arns" {
  value = {
    for k, m in module.lambda_tools : k => m.function_arn
  }
  description = "Map of engine name -> Lambda function ARN"
}

output "enabled_engines" {
  value       = local.enabled_engines
  description = "List of enabled search engine names"
  sensitive   = false
}

output "log_group_name" {
  value       = module.observability.log_group_name
  description = "CloudWatch Log Group name for vended logs"
}

# ============================================================
# Additional outputs for dashboard/cowork setup
# ============================================================

output "auth_m2m_client_id" {
  value       = module.auth.m2m_client_id
  description = "Cognito M2M Client ID (for service-to-service auth)"
  sensitive   = false
}

output "auth_issuer_url" {
  value       = module.auth.issuer_url
  description = "Cognito OIDC issuer URL"
  sensitive   = false
}

output "cognito_domain_url" {
  value       = module.auth.domain_url
  description = "Full Cognito hosted-UI/OAuth domain URL (token endpoint base)"
  sensitive   = false
}

output "auth_m2m_client_secret" {
  value       = module.auth.m2m_client_secret
  description = "Cognito M2M Client Secret (for client_credentials grant)"
  sensitive   = true
}

# Scope to request for the M2M client_credentials grant (gateway authorizer scope).
output "auth_m2m_scope" {
  value       = "${module.auth.resource_server_id}/invoke"
  description = "OAuth scope for M2M access tokens"
  sensitive   = false
}

output "dashboard_env" {
  value = {
    NEXT_PUBLIC_TOOL_GATEWAY_ENABLED = "true"
    NEXT_PUBLIC_TOOL_GATEWAY_URL     = module.gateway.gateway_url
    NEXT_PUBLIC_TOOL_GATEWAY_ID      = module.gateway.gateway_id
    NEXT_PUBLIC_TOOL_GATEWAY_REGION  = var.aws_region
    TOOL_GATEWAY_ARN                 = module.gateway.gateway_arn
    COGNITO_TOOL_TOKEN_ENDPOINT      = "${module.auth.domain_url}/oauth2/token"
    COGNITO_TOOL_M2M_CLIENT_ID       = module.auth.m2m_client_id
    COGNITO_TOOL_M2M_CLIENT_SECRET   = module.auth.m2m_client_secret
    COGNITO_TOOL_M2M_SCOPE           = "${module.auth.resource_server_id}/invoke"
  }
  sensitive   = true
  description = "Env map consumed by admin-ui Tool Gateway dashboard (provision script writes dashboard.generated.env)"
}
