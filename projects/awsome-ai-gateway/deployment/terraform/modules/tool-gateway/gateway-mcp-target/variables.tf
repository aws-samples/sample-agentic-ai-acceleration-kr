variable "mcp_server_targets" {
  type = map(object({
    endpoint            = string
    credential_provider = optional(string)
    description         = optional(string, "")
  }))
  description = "Map of target_name -> {endpoint, credential_provider} for external MCP servers"
  default     = {}
}
