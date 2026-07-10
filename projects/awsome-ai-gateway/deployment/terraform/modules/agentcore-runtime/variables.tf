# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type        = string
  description = "프로젝트 prefix (예: llm-gateway)"
}

variable "environment" {
  type        = string
  description = "환경 (dev | prod)"
}

variable "vpc_id" {
  type        = string
  description = "AgentCore Runtime 이 attach 될 VPC ID"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "AgentCore Runtime 의 ENI 가 부착될 사설 서브넷"
}

variable "agent_security_group_ids" {
  type        = list(string)
  description = "AgentCore Runtime ENI 에 적용할 SG. Aurora reader / S3 endpoint 접근 허용 필요"
  default     = []
}

variable "bedrock_allowed_model_arns" {
  type        = list(string)
  description = "Agent 가 호출하는 Bedrock 모델 ARN. global.anthropic.* inference profile + foundation model 양쪽 필요"
}

variable "cognito_user_pool_arn" {
  type        = string
  description = "Inbound JWT 인증에 쓰일 Cognito User Pool ARN"
}

variable "cognito_issuer_url" {
  type        = string
  description = "OIDC discovery URL (https://cognito-idp.{region}.amazonaws.com/{pool_id})"
}

variable "cognito_audiences" {
  type        = list(string)
  description = "허용되는 audience (client_id). admin-ui app client id"
}

variable "ecr_image_tag_mutability" {
  type        = string
  description = "ECR image tag mutability"
  default     = "IMMUTABLE"
}

variable "staging_lifecycle_days" {
  type        = number
  description = "S3 staging/ prefix(중간 데이터) 객체 자동 삭제 기간 (일)"
  default     = 1
}

variable "report_lifecycle_days" {
  type        = number
  description = "S3 reports/ prefix(다운로드 리포트) 객체 자동 삭제 기간 (일) — 다운로드 가능 창"
  default     = 7
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ── admin-chat-agent deterministic tools (query_db / get_schema Lambdas) ──
variable "enable_db_tools" {
  type        = bool
  description = "true 면 query_db/get_schema Lambda + reader secret + SG + IAM 생성. agent 가 boto3 로 직접 invoke."
  default     = false
}

variable "aurora_endpoint" {
  type        = string
  description = "query_db Lambda 가 접속할 Aurora(or RDS Proxy) endpoint host"
  default     = ""
}

variable "aurora_security_group_id" {
  type        = string
  description = "Aurora 의 SG ID. Lambda SG 를 이 SG 의 5432 ingress 로 허용"
  default     = ""
}

variable "db_name" {
  type        = string
  description = "Postgres DB 이름"
  default     = "gateway"
}
