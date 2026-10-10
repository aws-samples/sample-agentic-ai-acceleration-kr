output "alb_url" {
  description = "Public entry point. Open this once both ECS services are running."
  value       = local.https_enabled ? "https://${local.public_host}" : "http://${module.ecs.alb_dns_name}"
}

output "ecr_repository_urls" {
  description = "Push the server/web images here, then set server_image / web_image and re-apply."
  value       = module.ecr.repository_urls
}

output "cognito_user_pool_id" {
  value = module.cognito.user_pool_id
}

output "cognito_client_id" {
  value = module.cognito.client_id
}

output "cluster_name" {
  description = "Needed for `aws ecs update-service --force-new-deployment` after pushing a new image tag."
  value       = module.ecs.cluster_name
}

output "agent_registry_id" {
  description = "Set this as AGENT_REGISTRY_ID for local server runs. Empty when enable_agent_registry = false."
  value       = var.enable_agent_registry ? module.agent_registry[0].registry_id : ""
}

output "agent_registry_mcp_endpoint" {
  description = "The registry's MCP endpoint; connect an IDE through mcp-proxy-for-aws (--service agent-registry). Empty when the registry is off."
  value       = var.enable_agent_registry ? module.agent_registry[0].mcp_endpoint : ""
}

output "registry_approvals_topic_arn" {
  description = "SNS topic announcing record approval-workflow events; empty when the registry is off."
  value       = var.enable_agent_registry ? module.agent_registry[0].approvals_topic_arn : ""
}

output "mcp_gateway_url" {
  description = "Set this as MCP_GATEWAY_URL for the agent runtime (already without the /mcp suffix)."
  value       = module.mcp_gateway.gateway_url
}


output "guardrail_id" {
  description = "Set this as GUARDRAIL_ID for the agent runtime (see agent-runtime/.env)."
  value       = module.bedrock_guardrail.guardrail_id
}

output "guardrail_version" {
  description = "Set this as GUARDRAIL_VERSION for the agent runtime (DRAFT tracks latest)."
  value       = module.bedrock_guardrail.guardrail_version
}

output "harness_execution_role_arn" {
  description = "Set this as HARNESS_EXECUTION_ROLE_ARN for local server runs."
  value       = module.harness_role.role_arn
}

output "agent_runtime_role_arn" {
  description = "Set this as EXECUTION_ROLE in agent-runtime/.env so deploy.sh uses a role that already has InvokeGateway."
  value       = module.agent_runtime_role.role_arn
}

output "skills_bucket" {
  description = "Set this as SKILLS_BUCKET for local server runs."
  value       = module.s3_skills.bucket_name
}

output "artifacts_bucket" {
  description = "Set this as ARTIFACTS_BUCKET for local server runs."
  value       = module.s3_artifacts.bucket_name
}

output "artifacts_table" {
  description = "Set this as ARTIFACTS_TABLE for local server runs."
  value       = module.dynamodb.artifacts_table_name
}

output "usage_table_name" {
  value       = module.dynamodb.usage_table_name
  description = "Set this as USAGE_TABLE for local server runs."
}

output "knowledge_bucket" {
  description = "Set this as KNOWLEDGE_BUCKET for local server runs."
  value       = module.s3_knowledge.bucket_name
}

output "knowledge_table" {
  description = "Set this as KNOWLEDGE_TABLE for local server runs."
  value       = module.dynamodb.knowledge_table_name
}

output "kb_service_role_arn" {
  description = "Set this as KB_SERVICE_ROLE_ARN for local server runs."
  value       = module.knowledge_roles.kb_service_role_arn
}

output "kb_gateway_role_arn" {
  description = "Set this as KB_GATEWAY_ROLE_ARN for local server runs."
  value       = module.knowledge_roles.kb_gateway_role_arn
}

output "alerts_topic_arn" {
  value = module.alarms.topic_arn
}

output "active_cost_allocation_tags" {
  description = "Tag keys held Active in Billing; empty when activation is turned off."
  value = (
    length(module.cost_allocation_tags) > 0
    ? module.cost_allocation_tags[0].active_tag_keys
    : []
  )
}

output "runtime_mcp_endpoints" {
  description = "Runtime MCP servers attached to the gateway (name => endpoint). Use the URL as the MCP record's remote URL in the Agent Registry."
  value       = module.mcp_gateway.runtime_mcp_endpoints
}

output "team_harness_role_arns" {
  description = "team => harness execution role ARN; the server gets this as TEAM_EXECUTION_ROLES."
  value       = { for t, m in module.team_harness_roles : t => m.role_arn }
}

output "mcp_gateway_id" {
  value = module.mcp_gateway.gateway_id
}

output "mcp_gateway_arn" {
  description = "Resource of the Cedar policies in modules/gateway_policies."
  value       = module.mcp_gateway.gateway_arn
}

output "policy_engine_arn" {
  value = var.enable_gateway_policy ? module.policy_engine[0].engine_arn : ""
}

output "policy_engine_id" {
  value = var.enable_gateway_policy ? module.policy_engine[0].engine_id : ""
}
