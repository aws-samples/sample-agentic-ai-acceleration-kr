# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "ecr_repository_url" {
  description = "admin-chat-agent 이미지를 push 할 ECR URL"
  value       = aws_ecr_repository.chat_agent.repository_url
}

output "ecr_repository_arn" {
  value = aws_ecr_repository.chat_agent.arn
}

output "agent_execution_role_arn" {
  description = "AgentCore Runtime 의 execution role. CreateAgentRuntime 에 전달"
  value       = aws_iam_role.agent_execution.arn
}

output "agent_execution_role_name" {
  value = aws_iam_role.agent_execution.name
}

output "staging_bucket_name" {
  description = "SQL → Code Specialist 데이터 전달용 S3 staging bucket"
  value       = aws_s3_bucket.staging.bucket
}

output "staging_bucket_arn" {
  value = aws_s3_bucket.staging.arn
}

output "kms_key_arn" {
  description = "S3 staging + AgentCore 세션 암호화 KMS key ARN"
  value       = aws_kms_key.chat_agent.arn
}

output "agent_name" {
  description = "AgentCore Runtime 등록 시 사용할 agent name"
  value       = local.agent_name
}

# ── admin-chat-agent tool Lambdas (enable_db_tools=true 일 때만) ──
output "query_db_function_name" {
  description = "agent 의 LAMBDA_QUERY_DB env 에 넣을 함수 이름"
  value       = var.enable_db_tools ? aws_lambda_function.query_db[0].function_name : null
}

output "get_schema_function_name" {
  description = "agent 의 LAMBDA_GET_SCHEMA env 에 넣을 함수 이름"
  value       = var.enable_db_tools ? aws_lambda_function.get_schema[0].function_name : null
}

output "chat_reader_secret_arn" {
  description = "gateway_chat_reader 비밀번호 secret ARN (apply 후 ALTER ROLE + put-secret-value)"
  value       = var.enable_db_tools ? aws_secretsmanager_secret.chat_reader[0].arn : null
}

output "lambda_security_group_id" {
  value = var.enable_db_tools ? aws_security_group.lambda[0].id : null
}
