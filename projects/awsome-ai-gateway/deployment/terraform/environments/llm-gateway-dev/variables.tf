# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type    = string
  default = "llm-gateway"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "azs" {
  type    = list(string)
  default = ["ap-northeast-2a", "ap-northeast-2c"] # dev는 2 AZ (비용)
}

variable "vpc_cidr" {
  type    = string
  default = "10.30.0.0/16" # 신규 vanilla 환경 — 기존 llm-gateway-dev(10.20)와 분리
}

variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.1.0/24", "10.30.2.0/24"]
}

variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.101.0/24", "10.30.102.0/24"]
}

variable "database_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.201.0/24", "10.30.202.0/24"]
}

variable "elasticache_subnet_cidrs" {
  type    = list(string)
  default = ["10.30.211.0/24", "10.30.212.0/24"]
}

variable "eks_cluster_version" {
  # AWS EKS 는 minor version downgrade 불가. 한번 apply 된 버전 이상으로만 올릴 수 있음.
  type    = string
  default = "1.30"
}

variable "aurora_engine_version" {
  type    = string
  default = "16.11"
}

variable "aurora_prod_instance_class" {
  # dev는 Serverless v2 로 자동 대체 (module 내부 로직)
  type    = string
  default = "db.serverless"
}

variable "elasticache_prod_node_type" {
  # dev는 cache.t4g.small 로 자동 대체 (module 내부 로직)
  type    = string
  default = "cache.t4g.small"
}

# dev ElastiCache 노드 수(deepdive Q50 Phase4). 1=단일노드(기본·기존, replica/failover
# 없음·저비용). 2 로 올리면 primary+replica → failover/Multi-AZ 자동 활성(prod 전
# failover 드릴 가능, 노드 ~2배 비용). 평소엔 1 유지 권장, 테스트 시 tfvars 로 2.
variable "elasticache_dev_num_cache_clusters" {
  type    = number
  default = 1
}

variable "enable_rds_proxy" {
  description = "Aurora 앞단에 RDS Proxy 배치 (connection pool). dev/prod 모두 기본 true 로 통일 — gateway user 인증을 Terraform 이 자동 관리하므로 Proxy 기본 사용 가능. 꼭 필요한 경우만 `-var enable_rds_proxy=false` 로 내릴 것."
  type        = bool
  default     = true
}

variable "application_namespace" {
  type    = string
  default = "llm-gateway"
}

# ─── Cognito (OIDC IDP) ───
variable "cognito_domain_suffix" {
  description = "Hosted UI 도메인 suffix. 최종: {project}-{env}-{suffix}.auth.{region}.amazoncognito.com (전 세계 unique). 빈 값이면 account_id 로 자동 생성(vanilla-auth-<account_id>) — 신규 계정 이식 시 별도 지정 불필요."
  type        = string
  default     = ""
}

variable "cognito_callback_urls" {
  description = "OIDC redirect URI 화이트리스트. gateway-cli 의 PKCE callback 용. 사용자 PC 의 localhost 포트."
  type        = list(string)
  default = [
    "http://localhost:8090/callback",
    "http://localhost:8091/callback",
    "http://localhost:8092/callback",
  ]
}

variable "cognito_logout_urls" {
  description = "OIDC logout redirect URI"
  type        = list(string)
  default = [
    "http://localhost:8090/logout",
    "http://localhost:8091/logout",
    "http://localhost:8092/logout",
  ]
}

variable "cognito_groups" {
  description = "User groups. Claude_<team> 은 Default Department 하위 팀, Claude_<dept>_<team> 은 dept 자동 생성 후 team 매핑, ClaudeAdmin 은 admin 부트스트랩."
  type        = list(string)
  default     = ["Claude_AI-Center_S/W-Culture-Office", "Claude_test-department_aws-test", "ClaudeAdmin"]
}

variable "bedrock_allowed_model_arns" {
  # 애플리케이션은 `global.anthropic.*` (cross-region inference profile) 로 호출.
  # IAM 은 inference-profile + 호출될 foundation-model 양쪽에 InvokeModel 허용 필요.
  #
  # ⚠️ **거부는 "이름을 안 쓴 쪽"에서 난다.** 프로파일 패턴(`global.anthropic.claude-*`)은
  # 세대에 무관하게 매칭되므로, foundation-model 줄만 구세대에 고정돼 있으면 신모델이
  # 조용히 AccessDenied 가 된다 — 프로파일은 통과했는데 그 프로파일이 가리키는
  # foundation-model 이 막힌 것이다. 실측(2026-08-19, federation token 으로 이 목록을
  # 그대로 세션 정책에 넣어 실호출):
  #     claude-4-* 만 있던 목록  → global.anthropic.claude-opus-5   AccessDenied
  #                                  (resource: foundation-model/anthropic.claude-opus-5)
  #     아래 5·6행 추가 후        → opus-5 / sonnet-5 둘 다 200 OK
  # 그래서 **신모델 등록 마이그레이션(0027 등)을 넣을 때 이 목록도 같이 늘려야 한다.**
  # DB 에 alias 를 넣는 것만으로는 호출되지 않는다.
  type = list(string)
  default = [
    # Foundation models (실제 추론이 실행되는 리소스)
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-*",
    # Claude 5 (migration 0027). 세대 전체를 `claude-*-5*` 로 열지 않고 모델별로 적는 이유는
    # fable-5 를 IAM 에서도 계속 막아 두기 위해서다(0027 이 의도적으로 미등록 — apne2
    # 프로파일 부재). 실측으로 fable-5 는 이 목록에서 AccessDenied 유지됨을 확인했다.
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5*",
    "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5*",
    # Global cross-region inference profiles (application 이 호출하는 엔트리포인트)
    "arn:aws:bedrock:*::inference-profile/global.anthropic.claude-*",
    "arn:aws:bedrock:*:*:inference-profile/global.anthropic.claude-*",
    # APAC cross-region inference profile (예비, ap-northeast-2 전용)
    "arn:aws:bedrock:ap-northeast-2::inference-profile/apac.anthropic.claude-*",
  ]
}

variable "eks_access_entries" {
  type    = any
  default = {}
}

variable "cowork_role_arn" {
  # Cowork cross-account Mantle role (222, Tokyo Opus 4.8). gateway-proxy AssumeRole into it.
  # Must match model.routing_profiles.account_role_arn for client=cowork (migration 0009).
  # The 222 role's trust must allow this env's gateway-proxy IRSA + sts:ExternalId=cowork-bedrock.
  type    = string
  default = "arn:aws:iam::222233334444:role/llm-gateway-cowork-bedrock"
}

variable "claude_code_333_role_arn" {
  # Claude Code cross-account Bedrock NATIVE role (333). gateway-proxy AssumeRole into it,
  # builds a 333 bedrock-runtime client (boto3 invoke_model). Must match
  # model.routing_profiles.account_role_arn for client=claude-code (migration 0022).
  # The 333 role trust must allow this env's gateway-proxy IRSA + sts:ExternalId=claude-code-bedrock.
  type    = string
  default = "arn:aws:iam::333344445555:role/llm-gateway-claude-code-bedrock"
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ─── admin-chat-agent (Phase 1 부트스트랩 — 활성 시 ECR/S3/IAM 만 생성) ───
variable "enable_chat_agent" {
  description = "admin-chat-agent 인프라 (ECR + S3 staging + IAM + KMS) 생성 여부"
  type        = bool
  default     = false
}

# ─── admin-chat-agent BI tool Lambdas (query_db / get_schema) ───
# enable_chat_agent=true 가 선행 조건 (같은 모듈에 추가됨).
variable "enable_chat_db_tools" {
  description = "admin-chat-agent 의 query_db/get_schema Lambda + reader secret + SG 생성 여부"
  type        = bool
  default     = false
}
