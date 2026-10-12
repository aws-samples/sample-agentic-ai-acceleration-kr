terraform {
  required_providers {
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.50"
    }
  }
}

locals {
  gateway  = "AgentCore::Gateway::\"${var.gateway_arn}\""
  arn_like = "arn:aws:sts::${var.account_id}:assumed-role/"

  # Exact ARN or ARN + "/<session>" — never a bare prefix wildcard, which would
  # let a role whose name is a prefix of another match both.
  role_cond = { for r in toset(concat(var.platform_role_names, var.demo_expense_role_names)) : r =>
    "(principal.id == \"${local.arn_like}${r}\" || principal.id like \"${local.arn_like}${r}/*\")"
  }

  platform_cond = join(" || ", [for r in var.platform_role_names : local.role_cond[r]])

  demo_expense_cond = join(" || ", [for r in var.demo_expense_role_names : local.role_cond[r]])

  # The registry MCP server's tools (mcp_gateway's registry_target).
  registry_tools = [
    "search_discoverable_registry_records",
    "list_discoverable_registry_records",
    "batch_get_discoverable_registry_record",
  ]

  # AgentCore policy/engine names must match [A-Za-z][A-Za-z0-9_]* (max 48).
  name_prefix = replace(var.project, "-", "_")
}

# AWS_IAM gateways evaluate an AgentCore::IamEntity principal. An untyped
# `principal` also covers AgentCore::UnauthenticatedUser, which has no `id`, so
# FAIL_ON_ANY_FINDINGS rejects `principal.id` ("attribute `id` on entity type
# `AgentCore::UnauthenticatedUser` not found", 2026-10-10 live). Cloud Control
# only reports InternalFailure; the validator text is in ListPolicies
# statusReasons.

# Platform roles (runtime, default harness) may call every tool on this gateway.
resource "awscc_bedrockagentcore_policy" "platform" {
  name             = "${local.name_prefix}_platform_all_tools"
  policy_engine_id = var.policy_engine_id
  enforcement_mode = "ACTIVE"
  validation_mode  = "FAIL_ON_ANY_FINDINGS"
  definition = {
    cedar = {
      statement = "permit(principal is AgentCore::IamEntity, action, resource == ${local.gateway}) when { ${local.platform_cond} };"
    }
  }
}

# Team roles are not known here: the Organization page creates each team's role
# and its Cedar permit at runtime (server/services/team_provisioner.py), with
# the same principal condition as above. The platform permit covers every
# action, so without this a teamless caller (basic chat, a shared harness)
# could approve any amount or read salary bands — more than any team. Cedar
# forbid wins over permit, so the team-only tools stay with the team policies.
resource "awscc_bedrockagentcore_policy" "platform_team_only" {
  count            = length(var.team_only_actions) > 0 ? 1 : 0
  name             = "${local.name_prefix}_platform_team_only_forbid"
  policy_engine_id = var.policy_engine_id
  enforcement_mode = "ACTIVE"
  validation_mode  = "FAIL_ON_ANY_FINDINGS"
  definition = {
    cedar = {
      statement = "forbid(principal is AgentCore::IamEntity, action in [${join(", ", [for n in var.team_only_actions : "AgentCore::Action::\"${var.target_name}___${n}\""])}], resource == ${local.gateway}) when { ${local.platform_cond} };"
    }
  }
}

# The registry target searches the whole catalog with the gateway's own role, so
# it cannot apply the per-team visibility the server enforces on /registry: a
# teamless caller would list every team's records. Forbid it for the platform
# roles; a team gets it only if its allowed tools name it.
resource "awscc_bedrockagentcore_policy" "platform_registry" {
  count            = var.registry_target_name != "" ? 1 : 0
  name             = "${local.name_prefix}_platform_registry_forbid"
  policy_engine_id = var.policy_engine_id
  enforcement_mode = "ACTIVE"
  validation_mode  = "FAIL_ON_ANY_FINDINGS"
  definition = {
    cedar = {
      statement = "forbid(principal is AgentCore::IamEntity, action in [${join(", ", [for n in local.registry_tools : "AgentCore::Action::\"${var.registry_target_name}___${n}\""])}], resource == ${local.gateway}) when { ${local.platform_cond} };"
    }
  }
}

# Workshop demo: approve_expense for the named roles, only below the limit. The
# server cannot express an argument condition in the policies it generates, so
# this one stays in terraform; it matches roles by name (the assumed-role ARN
# has no path), so a role the Organization page created qualifies.
resource "awscc_bedrockagentcore_policy" "demo_expense_limit" {
  count            = length(var.demo_expense_role_names) > 0 ? 1 : 0
  name             = "${local.name_prefix}_demo_expense_limit"
  policy_engine_id = var.policy_engine_id
  enforcement_mode = "ACTIVE"
  validation_mode  = "FAIL_ON_ANY_FINDINGS"
  definition = {
    cedar = {
      statement = "permit(principal is AgentCore::IamEntity, action == AgentCore::Action::\"${var.target_name}___approve_expense\", resource == ${local.gateway}) when { (${local.demo_expense_cond}) && context.input.amount < ${var.expense_limit_usd} };"
    }
  }
}
