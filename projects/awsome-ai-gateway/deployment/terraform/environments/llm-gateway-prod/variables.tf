# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type    = string
  default = "llm-gateway"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "azs" {
  type    = list(string)
  default = ["ap-northeast-2a", "ap-northeast-2c"] # prod는 multi-AZ (HA)
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16" # prod 환경 — 기존 새 dev(10.30)와 분리
}

variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.1.0/24", "10.40.2.0/24"]
}

variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.101.0/24", "10.40.102.0/24"]
}

variable "database_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.201.0/24", "10.40.202.0/24"]
}

variable "elasticache_subnet_cidrs" {
  type    = list(string)
  default = ["10.40.211.0/24", "10.40.212.0/24"]
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
  # prod는 provisioned 인스턴스 (모듈이 prod일 때 serverlessv2_scaling_configuration={}로 보내고
  # instance_class를 var.prod_instance_class 그대로 사용 — 따라서 db.serverless 로 두면 모순).
  # 옛 prod에서 사용하던 동일 인스턴스 클래스. 부하 따라 r6g.xlarge 등으로 조정 가능.
  type    = string
  default = "db.r7g.large"
}

variable "elasticache_prod_node_type" {
  # prod는 큰 노드 (옛 prod 동일).
  type    = string
  default = "cache.r7g.large"
}

# ElastiCache prod HA(deepdive Q50 Phase4). 샤드당 replica 수 — 2 면 primary failover
# 시 zero-redundancy 윈도우 제거 + read 스케일(3샤드×3노드=9). 1 이면 기존(6노드).
# **비용 영향(+50% 노드시간)** — 운영 예산 결정 후 tfvars 로 명시. 모듈 기본 1 과 달리
# prod 권장값 2 를 이 env 기본으로 둔다(plan 으로 영향 확인 후 apply).
variable "elasticache_prod_replicas_per_node_group" {
  type    = number
  default = 2
}

# prod 커스텀 cluster 파라미터그룹(maxmemory-policy/reserved-memory). true 면 메모리압박
# noeviction OOM-거부 회피 + failover 헤드룸. 기존(false)은 AWS default.valkey7.cluster.on.
variable "elasticache_prod_enable_custom_param_group" {
  type    = bool
  default = true
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
  description = "User groups. Claude_<team> 은 Default Department 하위 팀, Claude_<dept>_<team> 은 dept 자동 생성 후 team 매핑, ClaudeAdmin 은 admin 부트스트랩. prod는 dev 검증에서 결정된 깨끗한 셋만 — 빈 팀 잔재(aws-test, S/W-Culture-Office) 미포함."
  type        = list(string)
  default     = ["Claude_AWS-AI-Specialist", "ClaudeAdmin"]
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

variable "tags" {
  type    = map(string)
  default = {}
}

# ─── admin-chat-agent (Phase 1 부트스트랩 — 활성 시 ECR/S3/IAM 만 생성) ───
# AgentCore Runtime 자체 (CreateAgentRuntime) 는 image push 후 별도 단계.
# 처음엔 false 로 두고, image 가 빌드/push 가능한 상태가 되면 true 로 enable.
variable "enable_chat_agent" {
  description = "admin-chat-agent 인프라 (ECR + S3 staging + IAM + KMS) 생성 여부"
  type        = bool
  default     = false
}
