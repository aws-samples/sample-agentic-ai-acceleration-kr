# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

output "cluster_name" {
  value = module.eks.cluster_name
}
output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}
output "cluster_oidc_provider_arn" {
  value = module.eks.oidc_provider_arn
}

output "gateway_proxy_role_arn" {
  value = module.irsa.gateway_proxy_role_arn
}
output "admin_api_role_arn" {
  value = module.irsa.admin_api_role_arn
}
output "all_role_arns" {
  value = module.irsa.all_role_arns
}

output "aurora_endpoint" {
  value = module.aurora.cluster_endpoint
}
output "aurora_master_user_secret_arn" {
  value     = module.aurora.master_user_secret_arn
  sensitive = true
}

# Terraform-managed DB secrets (enable_rds_proxy = true 시)
output "gateway_user_secret_arn" {
  value     = module.aurora.gateway_user_secret_arn
  sensitive = true
}
output "db_secret_arn" {
  value     = module.aurora.db_secret_arn
  sensitive = true
}

# RDS Proxy (enable_rds_proxy = true 시에만 값 존재)
output "rds_proxy_enabled" {
  value = module.aurora.proxy_enabled
}
output "rds_proxy_endpoint" {
  value = module.aurora.proxy_endpoint
}
# Helm values database.external.host 에 주입할 호스트 — Proxy on/off 에 관계없이 올바른 값.
output "application_db_endpoint" {
  value = module.aurora.application_endpoint
}

output "elasticache_endpoint" {
  value = module.elasticache.primary_endpoint_address
}
output "elasticache_auth_token_secret_arn" {
  value     = module.elasticache.auth_token_secret_arn
  sensitive = true
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "nat_public_ips" {
  value = module.vpc.nat_public_ips
}

output "kubectl_config_command" {
  value = "aws eks update-kubeconfig --region ${var.aws_region} --name ${module.eks.cluster_name}"
}

# ─── Cognito (OIDC IDP) ───
output "cognito_user_pool_id" {
  value = module.cognito.user_pool_id
}
output "cognito_client_id" {
  value = module.cognito.client_id
}
output "cognito_issuer_url" {
  value       = module.cognito.issuer_url
  description = "admin-api 의 OIDC_ISSUER_URL 값"
}
output "cognito_hosted_ui_domain" {
  value       = module.cognito.hosted_ui_domain
  description = "Cognito Hosted UI 도메인 (사용자 로그인 페이지 호스트)"
}
output "cognito_groups" {
  value       = module.cognito.groups
  description = "Cognito user groups — admin 이 이 그룹에 사용자를 추가"
}

# ─── admin-chat-agent (enable_chat_agent=true 일 때만) ───
output "chat_agent_ecr_url" {
  value       = try(module.agentcore_runtime[0].ecr_repository_url, null)
  description = "admin-chat-agent 컨테이너 image 를 push 할 ECR URL"
}
output "chat_agent_execution_role_arn" {
  value       = try(module.agentcore_runtime[0].agent_execution_role_arn, null)
  description = "AgentCore CreateAgentRuntime 호출 시 전달할 execution role ARN"
}
output "chat_agent_staging_bucket" {
  value       = try(module.agentcore_runtime[0].staging_bucket_name, null)
  description = "SQL → Code Specialist 데이터 전달용 S3 staging bucket"
}
output "chat_agent_name" {
  value       = try(module.agentcore_runtime[0].agent_name, null)
  description = "AgentCore Runtime 등록 시 사용할 agent name"
}

# ─── BI tool Lambdas (enable_chat_db_tools=true 일 때만) ───
output "chat_agent_query_db_function" {
  value       = try(module.agentcore_runtime[0].query_db_function_name, null)
  description = "agent 의 LAMBDA_QUERY_DB env 값"
}
output "chat_agent_get_schema_function" {
  value       = try(module.agentcore_runtime[0].get_schema_function_name, null)
  description = "agent 의 LAMBDA_GET_SCHEMA env 값"
}
output "chat_agent_reader_secret_arn" {
  value       = try(module.agentcore_runtime[0].chat_reader_secret_arn, null)
  description = "gateway_chat_reader 비밀번호 secret ARN (apply 후 password 설정 필요)"
}

# ─── Bedrock invocation logging (admin-api 감사 대조 설정값) ───
# helm values 의 `adminApi.env.BEDROCK_INVOCATION_LOG_GROUP` /
# `adminApi.env.BEDROCK_INVOCATION_LOG_REGION` 에 그대로 옮긴다(차트에 전용 블록은 없다 —
# admin-api 앱 설정은 전부 adminApi.env 자유 map 을 통과한다. values-eks-fargate-dev.yaml
# 의 adminApi.env 주석 참조). install-eks.sh 는 이 두 값을 자동 주입하지 않는다:
# 리전 단위로 계정 전체 본문을 수집하는 스위치라 운영자가 명시적으로 적어야 한다.
# 꺼져 있으면 logGroup 이 빈 문자열이고, admin-api 는 감사 엔드포인트를 404 가 아니라
# 503(미설정)으로 응답한다 — UI 가 "기능 없음" 과 "안 켬" 을 구분할 수 있어야 한다.
output "bedrock_invocation_log_group" {
  value       = module.bedrock_invocation_logging.log_group_name
  description = "admin-api BEDROCK_INVOCATION_LOG_GROUP 값 (미사용 시 빈 문자열)"
}
output "bedrock_invocation_log_region" {
  value       = module.bedrock_invocation_logging.log_region
  description = "admin-api BEDROCK_INVOCATION_LOG_REGION — 배포 리전이 아니라 모델 실행 리전(us-east-2)"
}
output "bedrock_invocation_log_bucket" {
  value       = module.bedrock_invocation_logging.large_body_bucket
  description = ">100KB 본문 sidecar S3 버킷. admin-api 는 읽지 않는다(권한 없음, 포인터만 노출)"
}

# ─── 게이트웨이 본문 로깅 sink (gateway-proxy 설정값) ───
# helm values 의 `gatewayProxy.env.FIREHOSE_STREAM_NAME` / `gatewayProxy.env.BODY_LOG_S3_BUCKET`
# 에 그대로 옮긴다(차트에 전용 블록은 없다 — gateway-proxy 앱 설정은 gatewayProxy.env
# 자유 map 을 통과한다). install-eks.sh 는 자동 주입하지 않는다: 마스킹되지 않은 본문을
# 저장하는 스위치라 운영자가 명시적으로 적어야 한다.
# 꺼져 있으면 빈 문자열이고, gateway-proxy 는 그것을 "미설정" 으로 읽어 로거를 no-op 으로
# 만든다 — 즉 이 두 값을 비우는 것이 코드 변경 없이 확실하게 끄는 방법이다.
output "body_log_firehose_stream" {
  value       = module.body_logging.firehose_stream_name
  description = "gateway-proxy FIREHOSE_STREAM_NAME 값 (미사용 시 빈 문자열 = 로깅 no-op)"
}
output "body_log_bucket" {
  value       = module.body_logging.bucket_name
  description = "gateway-proxy BODY_LOG_S3_BUCKET 값 (Firehose 레코드 상한 초과분 직행 fallback)"
}
