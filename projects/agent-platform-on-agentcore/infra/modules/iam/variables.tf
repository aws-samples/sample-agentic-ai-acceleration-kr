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

variable "registry_sync_role_arn" {
  description = "Role AWS Agent Registry assumes to SigV4-sign record synchronisation fetches (module agent_registry's sync role). Grants the server iam:PassRole on it so a record can be registered with an IAM-authenticated source; empty grants nothing."
  type        = string
  default     = ""
}

variable "runs_table_arn" {
  description = "Chat run leases table ARN (server/services/run_leases.py)."
  type        = string
}

variable "teams_table_arn" {
  description = "Teams/audit table ARN (Organization page). Required: an empty string inside a `resources` list is an invalid ARN."
  type        = string
}

variable "team_role_boundary_arn" {
  description = "Managed policy the server must attach as the permissions boundary of every team role it creates. Empty grants no IAM write."
  type        = string
  default     = ""
}

variable "policy_engine_arn" {
  description = "AgentCore policy engine the server writes per-team Cedar policies to. Empty grants nothing (teams are created without gateway policies)."
  type        = string
  default     = ""
}

variable "create_org_policy" {
  description = "Create the Organization-page managed policy (team role creation, pool user/group writes, Cedar policies). A plain bool so `count` is known at plan time on a fresh state; the three ARN inputs only decide which statements the document carries."
  type        = bool
  default     = true
}

variable "gateway_arn" {
  description = "Platform MCP gateway ARN the per-team Cedar policies name as their resource. Needed with policy_engine_arn: creating a gateway-scoped policy also authorises bedrock-agentcore:ManageResourceScopedPolicy on the gateway. Empty skips the statement."
  type        = string
  default     = ""
}
