variable "table_name" {
  type    = string
  default = "langgraph-threads"
}

variable "artifacts_table_name" {
  type    = string
  default = "agent-artifacts"
}

variable "knowledge_table_name" {
  type    = string
  default = "agent-knowledge-bases"
}

variable "usage_table_name" {
  description = "Per-turn usage rollup table"
  type        = string
}

variable "prefs_table_name" {
  description = "Per-user preferences table (dashboard layout, etc.)"
  type        = string
}

variable "users_table_name" {
  description = "OIDC (Entra ID) sign-in records, written on login so Insights can name those users"
  type        = string
}

variable "allow_destroy" {
  description = "Allow destruction of DynamoDB tables. Must be set to true and applied before running destroy to disable deletion protection."
  type        = bool
  default     = false
}

variable "teams_table_name" {
  type        = string
  description = "Team metadata, Entra group mappings and the org audit ledger (server/repositories/team_repository.py)."
}

variable "slack_table_name" {
  type        = string
  description = "Slack DM integration table: integrations, app uniqueness, event markers, thread locks (server/integrations/slack)."
}
