output "mcp_server_endpoints" {
  value = nonsensitive({
    for k, v in var.mcp_server_targets : k => v.endpoint
  })
  description = "Map of target_name -> endpoint for gateway target registration"
}

output "mcp_server_credentials" {
  value = nonsensitive({
    for k, v in var.mcp_server_targets : k => v.credential_provider
    if v.credential_provider != null
  })
  description = "Map of target_name -> credential_provider_arn for gateway target registration"
}
