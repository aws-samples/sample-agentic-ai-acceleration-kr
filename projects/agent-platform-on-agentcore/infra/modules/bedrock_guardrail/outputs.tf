output "guardrail_id" {
  description = "Set this as GUARDRAIL_ID for the agent runtime."
  value       = aws_bedrock_guardrail.this.guardrail_id
}

output "guardrail_arn" {
  value = aws_bedrock_guardrail.this.guardrail_arn
}

output "guardrail_version" {
  description = "Set this as GUARDRAIL_VERSION; DRAFT tracks the latest edits."
  value       = aws_bedrock_guardrail.this.version
}
