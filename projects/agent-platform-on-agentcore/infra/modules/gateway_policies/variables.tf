variable "project" {
  type = string
}

variable "policy_engine_id" {
  type = string
}

variable "gateway_arn" {
  type        = string
  description = "Resource every policy names. A policy that names an action must name a gateway."
}

variable "target_name" {
  type        = string
  description = "Lambda tools target name; Cedar actions are `<target_name>___<tool>`."
}

variable "account_id" {
  type = string
}

variable "platform_role_names" {
  type        = list(string)
  description = "IAM role names allowed to call every tool (the agent runtime role, the default harness role)."
}

variable "team_only_actions" {
  type        = list(string)
  description = "Lambda-target tool names (without the target prefix) the platform roles must not call: they belong to team policies only. Rendered as one forbid policy; empty = none. Each must exist on the target, or validation fails the policy."
  default     = []
}

variable "registry_target_name" {
  type        = string
  description = "Gateway target name of the registry MCP server, whose tools the platform roles must not call. Empty = no policy (the target is not attached; naming its tools would fail validation)."
  default     = ""
}

variable "demo_expense_role_names" {
  type        = list(string)
  description = "IAM role names permitted to call approve_expense below expense_limit_usd (workshop demo). Empty = no policy. The tool must exist on the target."
  default     = []
}

variable "expense_limit_usd" {
  type        = number
  description = "approve_expense is permitted only below this amount (context.input.amount)."
  default     = 10000
}
