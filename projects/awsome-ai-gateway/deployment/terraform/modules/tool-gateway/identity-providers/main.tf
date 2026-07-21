# ============================================================
# AgentCore API-Key Credential Providers (one per search engine)
# ============================================================
# These are created empty; actual API keys are seeded via
# scripts/seed-api-keys.sh using bedrock-agentcore-control CLI.

resource "aws_bedrockagentcore_api_key_credential_provider" "this" {
  for_each = var.api_key_providers

  name = each.key

  # Set a placeholder; actual API keys are populated by seed-api-keys.sh
  api_key_wo         = "placeholder"
  api_key_wo_version = "1"

  tags = {
    Component = "identity"
    Engine    = each.key
  }
}
