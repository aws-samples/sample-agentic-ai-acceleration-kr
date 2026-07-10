# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "user_pool_id" {
  value       = aws_cognito_user_pool.this.id
  description = "Cognito User Pool ID"
}

output "user_pool_arn" {
  value       = aws_cognito_user_pool.this.arn
  description = "Cognito User Pool ARN"
}

output "client_id" {
  value       = aws_cognito_user_pool_client.cli.id
  description = "App Client ID — gateway-cli 의 OIDC_CLIENT_ID"
}

output "issuer_url" {
  value       = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.this.id}"
  description = "OIDC issuer URL — admin-api 의 OIDC_ISSUER_URL"
}

output "hosted_ui_domain" {
  value       = "${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com"
  description = "Hosted UI 도메인. 사용자가 이걸로 로그인 페이지 접근."
}

output "groups" {
  value       = [for g in aws_cognito_user_group.groups : g.name]
  description = "생성된 user groups (admin 이 사용자를 여기 추가)"
}
