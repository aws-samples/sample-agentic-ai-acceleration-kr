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
  role_cond = { for r in toset(concat(var.platform_role_names, values(var.team_role_names))) : r =>
    "(principal.id == \"${local.arn_like}${r}\" || principal.id like \"${local.arn_like}${r}/*\")"
  }

  platform_cond = join(" || ", [for r in var.platform_role_names : local.role_cond[r]])

  team_cond = { for t, r in var.team_role_names : t => local.role_cond[r] }

  # AgentCore policy/engine names must match [A-Za-z][A-Za-z0-9_]* (max 48).
  name_prefix = replace(var.project, "-", "_")

  # The expense tool is governed by its own argument-conditioned policy below,
  # so it is left out of the plain per-team allow list.
  team_plain_tools = {
    for t, tools in var.team_tools : t => [for n in tools : n if n != "approve_expense"]
  }
  team_has_expense = { for t, tools in var.team_tools : t => contains(tools, "approve_expense") }

  # Every team role may also call the shared actions (e.g. the web-search
  # connector target's tool, which lives outside the Lambda target).
  all_team_cond = join(" || ", [for t, r in var.team_role_names : local.role_cond[r]])

  action_list = {
    for t, tools in local.team_plain_tools : t => join(", ", [
      for n in tools : "AgentCore::Action::\"${var.target_name}___${n}\""
    ])
  }
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

# Each team role: the team's plain tools.
resource "awscc_bedrockagentcore_policy" "team_tools" {
  for_each         = { for t, tools in local.team_plain_tools : t => tools if length(tools) > 0 }
  name             = "${local.name_prefix}_team_${replace(each.key, "-", "_")}_tools"
  policy_engine_id = var.policy_engine_id
  enforcement_mode = "ACTIVE"
  validation_mode  = "FAIL_ON_ANY_FINDINGS"
  definition = {
    cedar = {
      statement = "permit(principal is AgentCore::IamEntity, action in [${local.action_list[each.key]}], resource == ${local.gateway}) when { ${local.team_cond[each.key]} };"
    }
  }
}

# approve_expense: only teams that list it, and only below the limit.
resource "awscc_bedrockagentcore_policy" "expense_limit" {
  for_each         = { for t, has in local.team_has_expense : t => has if has }
  name             = "${local.name_prefix}_team_${replace(each.key, "-", "_")}_expense_limit"
  policy_engine_id = var.policy_engine_id
  enforcement_mode = "ACTIVE"
  validation_mode  = "FAIL_ON_ANY_FINDINGS"
  definition = {
    cedar = {
      statement = "permit(principal is AgentCore::IamEntity, action == AgentCore::Action::\"${var.target_name}___approve_expense\", resource == ${local.gateway}) when { ${local.team_cond[each.key]} && context.input.amount < ${var.expense_limit_usd} };"
    }
  }
}

# Actions outside the Lambda target that every team role may call. Each must be
# a real action on this gateway (`<target>___<tool>`): one unknown action fails
# validation for the whole policy.
resource "awscc_bedrockagentcore_policy" "team_shared" {
  count            = length(var.shared_actions) > 0 && length(var.team_role_names) > 0 ? 1 : 0
  name             = "${local.name_prefix}_team_shared_tools"
  policy_engine_id = var.policy_engine_id
  enforcement_mode = "ACTIVE"
  validation_mode  = "FAIL_ON_ANY_FINDINGS"
  definition = {
    cedar = {
      statement = "permit(principal is AgentCore::IamEntity, action in [${join(", ", [for a in var.shared_actions : "AgentCore::Action::\"${a}\""])}], resource == ${local.gateway}) when { ${local.all_team_cond} };"
    }
  }
}

# The platform permit above covers every action, so without this a teamless
# user (basic chat, a shared harness) could approve any amount or read salary
# bands — more than any team. Cedar forbid wins over permit, so the team-only
# tools stay with the team policies.
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
