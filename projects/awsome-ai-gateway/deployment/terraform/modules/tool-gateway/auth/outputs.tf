output "user_pool_id" {
  value       = aws_cognito_user_pool.main.id
  description = "Cognito User Pool ID"
}

output "user_pool_arn" {
  value       = aws_cognito_user_pool.main.arn
  description = "Cognito User Pool ARN"
}

output "domain" {
  value       = aws_cognito_user_pool_domain.main.domain
  description = "Cognito domain name"
}

output "domain_url" {
  value       = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
  description = "Cognito domain URL"
}

output "issuer_url" {
  value       = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  description = "Cognito OIDC issuer URL (discovery endpoint: issuer_url/.well-known/openid-configuration)"
}

output "app_client_id" {
  value       = aws_cognito_user_pool_client.app.id
  description = "Cognito App Client ID"
  sensitive   = false
}

output "app_client_secret" {
  value       = aws_cognito_user_pool_client.app.client_secret
  description = "Cognito App Client Secret"
  sensitive   = true
}

output "web_client_id" {
  value       = aws_cognito_user_pool_client.web.id
  description = "Cognito Web Client ID"
  sensitive   = false
}

output "m2m_client_id" {
  value       = aws_cognito_user_pool_client.m2m.id
  description = "Cognito M2M Client ID"
  sensitive   = false
}

output "m2m_client_secret" {
  value       = aws_cognito_user_pool_client.m2m.client_secret
  description = "Cognito M2M Client Secret"
  sensitive   = true
}

output "resource_server_id" {
  value       = aws_cognito_resource_server.agentcore.identifier
  description = "Cognito Resource Server ID"
}
