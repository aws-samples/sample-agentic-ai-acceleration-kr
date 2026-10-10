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

variable "team_role_names" {
  type        = map(string)
  description = "team => harness execution role name."
}

variable "team_tools" {
  type        = map(list(string))
  description = "team => tool names (without the target prefix) the team role may call."
}

variable "expense_limit_usd" {
  type        = number
  description = "approve_expense is permitted only below this amount (context.input.amount)."
  default     = 10000
}

variable "shared_actions" {
  type        = list(string)
  description = "Fully-qualified Cedar actions (`<target>___<tool>`) every team role may call, e.g. the web-search connector's `bap-web-search___WebSearch`. Empty = no shared policy."
  default     = []
}

variable "team_only_actions" {
  type        = list(string)
  description = "Lambda-target tool names (without the target prefix) the platform roles must not call: they belong to team policies only. Rendered as one forbid policy; empty = none. Each must exist on the target, or validation fails the policy."
  default     = ["approve_expense", "lookup_salary"]
}
