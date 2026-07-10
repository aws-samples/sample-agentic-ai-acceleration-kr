# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "oidc_provider_arn" {
  description = "EKS OIDC provider ARN (eks-fargate 모듈 output)"
  type        = string
}

variable "k8s_namespace" {
  description = "Gateway 애플리케이션 네임스페이스"
  type        = string
  default     = "llm-gateway"
}

variable "bedrock_allowed_model_arns" {
  description = "Bedrock InvokeModel 허용 모델 ARN 리스트. *.arn 으로 모델 고정 — 개인 모델 임의 사용 차단"
  type        = list(string)
  # 예시 (환경별 tfvars에서 실제 값 주입):
  # default = [
  #   "arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.claude-sonnet-4-*",
  #   "arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.claude-haiku-4-*",
  # ]
}

variable "secrets_manager_kms_key_arns" {
  description = "Secrets Manager가 쓰는 KMS 키 ARN (기본 AWS 관리 키 + 사용자 키)"
  type        = list(string)
  default     = ["*"]
}

variable "cognito_user_pool_arn" {
  description = "Cognito User Pool ARN — admin-api sync 기능에 필요"
  type        = string
  default     = ""
}

variable "cowork_role_arn" {
  description = <<-EOT
    Cowork cross-account Mantle role ARN (e.g. arn:aws:iam::234567890123:role/llm-gateway-cowork-bedrock).
    gateway-proxy AssumeRole into this to mint Mantle bearer for cowork -> Opus 4.8 (Tokyo).
    Must match model.routing_profiles.account_role_arn for client=cowork. The target role's trust
    policy must allow this account's gateway-proxy IRSA principal + sts:ExternalId=cowork-bedrock.
    Empty = cowork cross-account disabled (no AssumeRole statement rendered).
  EOT
  type        = string
  default     = ""
}

variable "claude_code_374_role_arn" {
  description = <<-EOT
    Claude Code cross-account Bedrock NATIVE role ARN (e.g. arn:aws:iam::345678901234:role/...).
    gateway-proxy AssumeRole into this to build a 374 bedrock-runtime client (boto3 invoke_model).
    Must match model.routing_profiles.account_role_arn for client=claude-code. The target role's
    trust must allow this account's gateway-proxy IRSA principal + sts:ExternalId=claude-code-bedrock.
    Empty = claude-code stays in-account (859); no AssumeRole statement rendered.
  EOT
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
