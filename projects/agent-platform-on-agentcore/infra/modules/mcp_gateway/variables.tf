variable "project" {
  type        = string
  description = "Project name prefix."
}

variable "region" {
  type        = string
  description = "AWS region hosting the gateway."
}

variable "web_search_backend" {
  type        = string
  description = "Web search on the gateway: agentcore (native AgentCore Web Search connector, no API key) or none."
  default     = "agentcore"

  validation {
    condition     = contains(["agentcore", "none"], var.web_search_backend)
    error_message = "web_search_backend must be one of: agentcore, none."
  }
}

variable "web_search_connector_version" {
  type        = string
  description = "Semantic version to pin the native web-search connector target to (e.g. 1.2.0, which adds request-level domain and published-date filters). Empty lets the gateway pick the connector's default version. Only used when web_search_backend = agentcore. Requires botocore >= 1.43.78 wherever terraform apply runs."
  default     = "1.2.0"
}

variable "runtime_mcp_servers" {
  type        = map(string)
  description = "MCP servers hosted on AgentCore Runtime to attach as gateway targets: target name => runtime ARN (e.g. { \"platform-status\" = \"arn:aws:bedrock-agentcore:...:runtime/bap_platform_status-...\" }). Each becomes an mcpServer target signed with the gateway role. Empty attaches none."
  default     = {}
}

variable "registry_mcp_endpoint" {
  type        = string
  description = "AWS Agent Registry MCP endpoint (module agent_registry's mcp_endpoint) to attach as an mcpServer target named \"registry\", so every agent and harness on this gateway can search the catalog at run time (search/list/batch-get tools). Empty attaches nothing."
  default     = ""
}

variable "registry_arn" {
  type        = string
  description = "ARN of that registry; scopes the gateway role's InvokeRegistryMcp/Search/List/Get grants. Required when registry_mcp_endpoint is set."
  default     = ""
}

variable "attach_registry" {
  type        = bool
  description = "Create the \"registry\" target. Separate from registry_mcp_endpoint because that value comes from a data source that is unknown while the registry module is being (re)applied, and count cannot depend on an unknown."
  default     = false
}

variable "policy_engine_arn" {
  type        = string
  description = "AgentCore policy engine to evaluate every tools/call on this gateway. Empty = no policy engine (every call allowed, as before)."
  default     = ""
}

variable "policy_mode" {
  type        = string
  description = "LOG_ONLY records the decision every call would get; ENFORCE applies it. Ignored when policy_engine_arn is empty."
  default     = "LOG_ONLY"

  validation {
    condition     = contains(["LOG_ONLY", "ENFORCE"], var.policy_mode)
    error_message = "policy_mode must be LOG_ONLY or ENFORCE."
  }
}

variable "demo_tools" {
  type        = bool
  description = "Keep the workshop mock tools (approve_expense, lookup_salary) on the Lambda target. They return canned results, so they are dropped by default."
  default     = false
}
