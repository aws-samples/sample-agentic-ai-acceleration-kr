output "credential_provider_arns" {
  value       = { for k, v in aws_bedrockagentcore_api_key_credential_provider.this : k => v.credential_provider_arn }
  description = "Map of provider_name -> credential_provider_arn"
}

output "credential_provider_names" {
  value       = keys(aws_bedrockagentcore_api_key_credential_provider.this)
  description = "List of credential provider names"
}
