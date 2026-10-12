output "policy_ids" {
  value = merge(
    { platform = awscc_bedrockagentcore_policy.platform.policy_id },
    { for p in awscc_bedrockagentcore_policy.platform_team_only : "platform-team-only" => p.policy_id },
    { for p in awscc_bedrockagentcore_policy.demo_expense_limit : "demo-expense-limit" => p.policy_id },
  )
}
