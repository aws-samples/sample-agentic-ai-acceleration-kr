# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# llm-gateway (vanilla) — Development 환경
# ------------------------------------------------------------------------------
# Prod와 동일 모듈을 쓰되, var.environment="dev" 로 내부에서 cheaper 설정 자동 선택
# ==============================================================================

# 다른 계정 이식성: account_id 를 동적 조회해 전역 unique 가 필요한 리소스명
# (Cognito Hosted UI 도메인 등)을 자동 생성. 신규 계정은 tfvars 수정 없이 동작.
data "aws_caller_identity" "current" {}

locals {
  # Cognito 도메인 suffix 미지정("")이면 account_id 로 자동 생성(전 세계 unique 보장 —
  # 신규 계정 forgot-to-override 방지). 명시하면 그 값 사용(기존 환경 호환).
  cognito_domain_suffix = (
    var.cognito_domain_suffix != ""
    ? var.cognito_domain_suffix
    : "vanilla-auth-${data.aws_caller_identity.current.account_id}"
  )
}

module "vpc" {
  source = "../../modules/vpc"

  project                  = var.project
  environment              = var.environment
  aws_region               = var.aws_region
  cidr                     = var.vpc_cidr
  azs                      = var.azs
  private_subnet_cidrs     = var.private_subnet_cidrs
  public_subnet_cidrs      = var.public_subnet_cidrs
  database_subnet_cidrs    = var.database_subnet_cidrs
  elasticache_subnet_cidrs = var.elasticache_subnet_cidrs

  tags = var.tags
}

module "eks" {
  source = "../../modules/eks-fargate"

  project     = var.project
  environment = var.environment
  # 두 값은 minor 홉마다 **함께** 움직인다. 순서/기한은 variables.tf 의
  # eks_cluster_version · eks_addon_versions 주석 참고(현재 1.31, 목표 1.36).
  cluster_version     = var.eks_cluster_version
  addon_versions      = var.eks_addon_versions
  vpc_id              = module.vpc.vpc_id
  private_subnet_ids  = module.vpc.private_subnet_ids
  public_access_cidrs = ["0.0.0.0/0"] # dev는 공개 접근 허용

  application_namespace = var.application_namespace
  access_entries        = var.eks_access_entries

  tags = var.tags
}

module "irsa" {
  source = "../../modules/irsa"

  project                    = var.project
  environment                = var.environment
  oidc_provider_arn          = module.eks.oidc_provider_arn
  k8s_namespace              = var.application_namespace
  bedrock_allowed_model_arns = var.bedrock_allowed_model_arns
  cognito_user_pool_arn      = module.cognito.user_pool_arn
  cowork_role_arn            = var.cowork_role_arn
  claude_code_374_role_arn   = var.claude_code_374_role_arn
  mantle_regions             = var.mantle_regions

  # 감사 대조(/admin/audit/invocation-log/*)용 Logs Insights 읽기 권한. 로깅이 꺼져 있으면
  # 빈 문자열이 와서 권한 statement 자체가 렌더되지 않는다(불필요한 권한을 남기지 않는다).
  bedrock_invocation_log_group_arn = module.bedrock_invocation_logging.log_group_arn

  # 본문 로깅 sink 쓰기 권한(쓰기 전용). body_logging 이 꺼져 있으면 빈 문자열이 와서
  # statement 가 렌더되지 않는다.
  body_log_firehose_arn = module.body_logging.firehose_stream_arn
  body_log_bucket_arn   = module.body_logging.bucket_arn

  tags = var.tags
}

# ─── 요청/응답 본문 로깅 sink (Firehose → S3) ───
# ⚠️ 여기 담기는 것은 **마스킹되지 않은** 프롬프트/응답 본문이다. 그래서 기본이 false 고,
#    켜도 그것만으로는 수집이 시작되지 않는다 — gateway-proxy 의 두 번째 잠금(관리자
#    런타임 토글, /monitoring 화면)이 기본 OFF 다. 즉 인프라 opt-in + 운영자 opt-in 둘 다
#    필요하고, 켜는 조작은 audit.audit_logs 에 남는다.
# ⚠️ AWS 네이티브 invocation logging(위 bedrock_invocation_logging)과 다른 것이다.
#    그쪽은 계정×리전 단위 AWS 설정이고 Mantle 트래픽을 전혀 잡지 못한다. 이 sink 는
#    게이트웨이가 직접 쓰므로 두 평면을 모두 덮는다.
module "body_logging" {
  source = "../../modules/body-logging"

  enabled     = var.enable_body_logging
  project     = var.project
  environment = var.environment

  log_retention_days = var.body_log_retention_days
  # dev 라도 false 다 — 버킷 내용물이 프롬프트 본문이므로 destroy 로 조용히 비워지게
  # 두지 않는다. 정말 필요하면 tfvars 에서 명시적으로 켠다.
  force_destroy = false

  tags = var.tags
}

# ─── Bedrock model-invocation logging (GPT-5.6 runtime plane 본문 감사) ───
# ⚠️ 이 모듈이 켜지면 us-east-2 **계정 전체** 의 Bedrock 요청/응답 본문이 수집된다.
#    provider 를 별칭으로 명시 전달하는 것이 필수다(모듈 versions.tf 의
#    configuration_aliases) — 기본 provider(서울) 상속을 문법 수준에서 막는다.
# ⚠️ default 는 false 다. dev us-east-2 는 현재 provision_bedrock_invocation_logging.py
#    가 소유하고 있으므로, 켜기 전에 variables.tf 의 import 절차를 따라야 한다.
module "bedrock_invocation_logging" {
  source = "../../modules/bedrock-invocation-logging"
  providers = {
    aws.logs = aws.bedrock_openai
  }

  enabled     = var.enable_bedrock_invocation_logging
  project     = var.project
  environment = var.environment

  log_region                = var.bedrock_invocation_log_region
  log_retention_days        = var.bedrock_invocation_log_retention_days
  large_body_retention_days = var.bedrock_invocation_log_retention_days
  # text 만 — 우리가 감사하는 것은 프롬프트/응답 텍스트이고, GPT-5.6 경로에는 image/video
  # modality 가 없어 켜면 sidecar 용량만 늘고 얻는 게 없다.
  modalities = ["text"]

  tags = var.tags
}

module "alb_controller" {
  source = "../../modules/alb-controller"

  project           = var.project
  environment       = var.environment
  cluster_name      = module.eks.cluster_name
  oidc_provider_arn = module.eks.oidc_provider_arn
  vpc_id            = module.vpc.vpc_id
  aws_region        = var.aws_region

  tags = var.tags

  # Fargate 프로파일이 ACTIVE 된 뒤에 helm_release 를 시작해야 한다.
  # 위 인자(cluster_name/oidc_provider_arn/vpc_id)는 클러스터 생성 직후 확정되므로
  # 이것만으로는 fargate_profiles·cluster_addons 에 대한 의존이 그래프에 안 잡히고,
  # terraform 이 프로파일 생성과 helm_release 를 병렬로 돌린다.
  # Fargate 는 Pod 를 만들 때 매칭되는 프로파일이 있어야 fargate-scheduler 에
  # 배정한다. 프로파일보다 먼저 생긴 Pod 는 기본 스케줄러에 배정되어, 노드가 없는
  # Fargate 전용 클러스터에서 영원히 Pending 으로 남는다(나중에 프로파일이 ACTIVE
  # 돼도 구제되지 않음) → helm timeout(900s) → atomic 으로 uninstall → apply 실패.
  # 재실행하면 프로파일이 이미 ACTIVE 라 통과하는 것이 이 문제의 증상이다.
  depends_on = [module.eks]
}

module "external_secrets" {
  source = "../../modules/external-secrets"

  project       = var.project
  environment   = var.environment
  irsa_role_arn = module.irsa.external_secrets_role_arn
  aws_region    = var.aws_region

  tags = var.tags
  # ALB Controller 의 webhook (mutating) 이 ESO Service 생성 시 호출되므로
  # ALB Controller Helm release 완료 이후에 ESO 가 설치되어야 한다.
  # enableServiceMutatorWebhook=false 로 근본 차단했지만, 설치 순서도 명시적으로 강제.
  depends_on = [module.eks, module.alb_controller]
}

module "aurora" {
  source = "../../modules/aurora-postgresql"

  project              = var.project
  environment          = var.environment
  engine_version       = var.aurora_engine_version
  vpc_id               = module.vpc.vpc_id
  db_subnet_group_name = module.vpc.database_subnet_group_name
  private_subnet_cidrs = module.vpc.private_subnet_cidrs
  availability_zones   = var.azs
  prod_instance_class  = var.aurora_prod_instance_class

  # RDS Proxy — dev 기본 off. prod 부하 테스트 전 스모크 검증 시에만 잠깐 true.
  enable_rds_proxy         = var.enable_rds_proxy
  proxy_private_subnet_ids = module.vpc.private_subnet_ids

  tags = var.tags
}

module "elasticache" {
  source = "../../modules/elasticache-valkey"

  project              = var.project
  environment          = var.environment
  vpc_id               = module.vpc.vpc_id
  subnet_group_name    = module.vpc.elasticache_subnet_group_name
  private_subnet_cidrs = module.vpc.private_subnet_cidrs
  prod_node_type       = var.elasticache_prod_node_type

  # dev 노드 수(기본 1=단일노드). 2 면 replica+failover 자동(deepdive Q50 Phase4).
  dev_num_cache_clusters = var.elasticache_dev_num_cache_clusters

  tags = var.tags
}

# ------------------------------------------------------------------------------
# Cognito User Pool (OIDC IDP)
# ------------------------------------------------------------------------------
module "cognito" {
  source = "../../modules/cognito"

  project       = var.project
  environment   = var.environment
  aws_region    = var.aws_region
  domain_suffix = local.cognito_domain_suffix
  callback_urls = var.cognito_callback_urls
  logout_urls   = var.cognito_logout_urls
  groups        = var.cognito_groups

  tags = var.tags
}

# Application namespace 는 install-eks.sh 또는 helm install --create-namespace 가 만듭니다.
# Terraform 에서 만들면 kubernetes provider 의 API 연결 타이밍 이슈가 생길 수 있어 제외.

# ------------------------------------------------------------------------------
# AgentCore Runtime — admin-chat-agent (BI assistant, 5-agent Strands)
# ------------------------------------------------------------------------------
module "agentcore_runtime" {
  count  = var.enable_chat_agent ? 1 : 0
  source = "../../modules/agentcore-runtime"

  project            = var.project
  environment        = var.environment
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids

  bedrock_allowed_model_arns = var.bedrock_allowed_model_arns
  cognito_user_pool_arn      = module.cognito.user_pool_arn
  cognito_issuer_url         = module.cognito.issuer_url
  cognito_audiences          = [module.cognito.client_id]

  staging_lifecycle_days = 1

  # ── BI tool Lambdas (query_db / get_schema) ──
  # query_db 는 Aurora cluster endpoint 로 직접 접속 (RDS Proxy 우회).
  # 이유: (1) admin BI 는 저빈도라 pooling 불필요, (2) proxy 는 gateway user 만
  # auth 등록돼 있어 chat_reader 는 proxy 통과 불가, (3) 공유 proxy 미변경.
  # → Lambda SG 를 cluster SG 의 5432 ingress 로 허용.
  enable_db_tools          = var.enable_chat_db_tools
  aurora_endpoint          = module.aurora.cluster_endpoint
  aurora_security_group_id = module.aurora.security_group_id
  db_name                  = "gateway"

  tags = var.tags
}
