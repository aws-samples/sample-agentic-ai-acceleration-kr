output "user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.this.arn
}

output "client_id" {
  value = aws_cognito_user_pool_client.this.id
}

output "domain" {
  description = "Hosted UI domain prefix (empty when not configured)."
  value       = one(aws_cognito_user_pool_domain.this[*].domain)
}
