output "registry_id" {
  description = "Registry ID, passed to the server as AGENT_REGISTRY_ID."
  value       = data.external.registry.result.registry_id
}

output "registry_arn" {
  value = data.external.registry.result.registry_arn
}

output "registry_name" {
  value = local.registry_name
}

output "mcp_endpoint" {
  description = "The registry's MCP endpoint (search/list/batch-get as MCP tools; SigV4, service agent-registry)."
  value       = data.external.registry.result.mcp_endpoint
}

output "sync_role_arn" {
  description = "Role AWS assumes to sign synchronisation fetches; empty when create_sync_role = false."
  value       = var.create_sync_role ? aws_iam_role.sync[0].arn : ""
}

output "approvals_topic_arn" {
  description = "SNS topic receiving record approval-workflow events; empty when disabled."
  value       = var.enable_approval_events ? aws_sns_topic.approvals[0].arn : ""
}
