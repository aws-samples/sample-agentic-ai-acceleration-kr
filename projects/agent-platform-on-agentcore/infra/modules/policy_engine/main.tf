terraform {
  required_providers {
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.50"
    }
  }
}

# One engine for the platform. Policies attach to the engine; gateways attach to
# the engine with a mode (LOG_ONLY records decisions, ENFORCE applies them). Kept
# in its own module so the gateway (which needs the engine ARN) and the policies
# (which need the gateway ARN) never form a module cycle.
resource "awscc_bedrockagentcore_policy_engine" "this" {
  # Name pattern [A-Za-z][A-Za-z0-9_]* (max 48): no hyphens.
  name = "${replace(var.project, "-", "_")}_gateway_policy"
}
