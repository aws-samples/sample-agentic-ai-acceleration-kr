# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# llm-gateway (vanilla) — Production 환경
# ==============================================================================

# 다른 계정 이식성: account_id 동적 조회 → 전역 unique 리소스명 자동 생성.
data "aws_caller_identity" "current" {}

locals {
  # Cognito 도메인 suffix 미지정("")이면 account_id 로 자동 생성(전 세계 unique).
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

  project             = var.project
  environment         = var.environment
  cluster_version     = var.eks_cluster_version
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

  # HA(deepdive Q50 Phase4) — replicas 2(zero-redundancy 윈도우 제거) + 커스텀
  # 파라미터그룹(maxmemory-policy/reserved-memory). apply 전 plan 으로 노드 증설 확인.
  prod_replicas_per_node_group   = var.elasticache_prod_replicas_per_node_group
  prod_enable_custom_param_group = var.elasticache_prod_enable_custom_param_group

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
# ECR + S3 staging + IAM role + KMS 까지 사전 준비. Runtime 자체 (CreateAgentRuntime)
# 는 image push 후 별도 단계.
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

  tags = var.tags
}
