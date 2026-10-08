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
