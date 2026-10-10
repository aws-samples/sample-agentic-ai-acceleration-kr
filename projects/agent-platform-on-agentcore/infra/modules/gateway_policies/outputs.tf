output "policy_ids" {
  value = merge(
    { platform = awscc_bedrockagentcore_policy.platform.policy_id },
    { for t, p in awscc_bedrockagentcore_policy.team_tools : "team-${t}" => p.policy_id },
    { for t, p in awscc_bedrockagentcore_policy.expense_limit : "expense-${t}" => p.policy_id },
    { for p in awscc_bedrockagentcore_policy.team_shared : "team-shared" => p.policy_id },
    { for p in awscc_bedrockagentcore_policy.platform_team_only : "platform-team-only" => p.policy_id },
  )
}
