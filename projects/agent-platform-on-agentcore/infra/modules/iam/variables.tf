variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "dynamodb_table_arn" {
  type = string
}

variable "skills_bucket_arn" {
  type = string
}

variable "artifacts_table_arn" {
  type = string
}

variable "artifacts_bucket_arn" {
  type = string
}

variable "harness_execution_role_arn" {
  type        = string
  description = "Role the ECS task may pass to AgentCore when creating a harness."
}

variable "knowledge_bucket_arn" {
  type = string
}

variable "knowledge_table_arn" {
  type = string
}

variable "kb_service_role_arn" {
  type        = string
  description = "Role the ECS task may pass to Bedrock when creating a knowledge base."
}

variable "kb_gateway_role_arn" {
  type        = string
  description = "Role the ECS task may pass to AgentCore when creating a KB gateway."
}

variable "browser_screenshot_bucket_arn" {
  type        = string
  default     = ""
  description = "The built-in-tools gateway's screenshot bucket, created outside Terraform by infra/builtin_tools_gateway/deploy.py. Read-only, so the chat can re-fetch a screenshot after the Lambda's presigned URL expires. Empty skips the statement, which leaves the route reporting 503."
}

variable "knowledge_source_bucket_arn" {
  type        = string
  default     = ""
  description = "The platform's kb-source bucket, where managed S3 knowledge bases keep per-user folders. The server writes site uploads here; external source buckets deliberately get no grant. Empty skips the statement."
}

variable "usage_table_arn" {
  description = "Usage rollup table ARN"
  type        = string
}

variable "prefs_table_arn" {
  description = "Per-user preferences table ARN"
  type        = string
}

variable "users_table_arn" {
  description = "OIDC sign-in records table ARN (UpdateItem on login, Scan for the Insights directory)"
  type        = string
}

variable "user_pool_arn" {
  description = "Cognito user pool ARN. Grants the server ListUsers on it so the admin Insights views can show a sub as an email; empty grants nothing."
  type        = string
  default     = ""
}
