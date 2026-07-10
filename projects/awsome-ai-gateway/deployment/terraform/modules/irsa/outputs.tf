# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "gateway_proxy_role_arn" {
  description = "gateway-proxy ServiceAccount에 박을 role ARN"
  value       = module.gateway_proxy_irsa.iam_role_arn
}

output "admin_api_role_arn" {
  description = "admin-api ServiceAccount에 박을 role ARN"
  value       = module.admin_api_irsa.iam_role_arn
}

output "external_secrets_role_arn" {
  description = "external-secrets controller ServiceAccount에 박을 role ARN"
  value       = module.external_secrets_irsa.iam_role_arn
}

output "all_role_arns" {
  description = "전체 IRSA role ARN (install 스크립트에서 한 번에 읽기용)"
  value = {
    gateway_proxy    = module.gateway_proxy_irsa.iam_role_arn
    admin_api        = module.admin_api_irsa.iam_role_arn
    external_secrets = module.external_secrets_irsa.iam_role_arn
  }
}
