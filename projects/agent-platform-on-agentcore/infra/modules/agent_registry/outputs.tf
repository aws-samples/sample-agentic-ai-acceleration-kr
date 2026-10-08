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
