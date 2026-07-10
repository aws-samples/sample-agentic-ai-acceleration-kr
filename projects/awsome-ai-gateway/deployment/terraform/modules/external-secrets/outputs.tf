# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "cluster_secret_store_name" {
  description = "ClusterSecretStore name - Helm values의 externalSecrets.secretStoreRef.name 에 입력"
  value       = "aws-secrets-manager"
}

output "namespace" {
  description = "External Secrets Operator 가 설치된 네임스페이스"
  value       = "external-secrets"
}
