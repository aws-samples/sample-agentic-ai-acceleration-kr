# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  value = module.eks.cluster_certificate_authority_data
}

output "cluster_version" {
  value = module.eks.cluster_version
}

# IRSA 모듈이 필수로 사용
output "oidc_provider_arn" {
  description = "OIDC provider ARN — IRSA 모듈의 oidc_providers 입력"
  value       = module.eks.oidc_provider_arn
}

output "oidc_provider_url" {
  description = "OIDC provider URL (https:// 제외)"
  value       = module.eks.oidc_provider
}

output "cluster_security_group_id" {
  value = module.eks.cluster_security_group_id
}

output "node_security_group_id" {
  value = module.eks.node_security_group_id
}

output "kms_key_arn" {
  value = aws_kms_key.eks.arn
}

output "fargate_profile_arns" {
  value = [for k, v in module.eks.fargate_profiles : v.fargate_profile_arn]
}
