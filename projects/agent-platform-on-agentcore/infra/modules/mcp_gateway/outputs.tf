output "gateway_url" {
  description = "MCP endpoint. Pass to the runtime as MCP_GATEWAY_URL (without the /mcp suffix)."
  # AgentCore's gateway_url ends in `/mcp`, but the runtime appends `/mcp` itself
  # and deploy.sh exports this output verbatim, so passing it raw yielded
  # `/mcp/mcp` → 400 → zero tools loaded. Strip it here so the output actually
  # matches the contract its description states.
  value = trimsuffix(awscc_bedrockagentcore_gateway.this.gateway_url, "/mcp")
}

output "gateway_id" {
  value = awscc_bedrockagentcore_gateway.this.gateway_identifier
}

output "gateway_arn" {
  value = awscc_bedrockagentcore_gateway.this.gateway_arn
}

output "tools_lambda_arn" {
  value = aws_lambda_function.tools.arn
}

output "runtime_mcp_endpoints" {
  description = "Runtime MCP endpoints attached as targets (name => URL). The same URL goes in the MCP record's remote URL when registering the server in the Agent Registry."
  value       = local.runtime_mcp_endpoints
}
