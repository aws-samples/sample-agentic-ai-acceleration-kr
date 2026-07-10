# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# VPC Module — terraform-aws-modules/vpc/aws wrapper
# ------------------------------------------------------------------------------
# 조직 표준(태그, Flow Log, NAT 정책)을 고정하고, dev/prod 환경 차이는 var로 주입.
# EKS Fargate에 필요한 subnet 태그를 자동 주입.
# ==============================================================================

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = "${var.project}-${var.environment}"
  cidr = var.cidr

  azs             = var.azs
  private_subnets = var.private_subnet_cidrs
  public_subnets  = var.public_subnet_cidrs
  # Aurora/ElastiCache 전용 격리 subnet (인터넷 접근 불필요)
  database_subnets    = var.database_subnet_cidrs
  elasticache_subnets = var.elasticache_subnet_cidrs

  # NAT — prod는 AZ별 분리, dev는 비용 절감 위해 단일 NAT
  enable_nat_gateway     = true
  single_nat_gateway     = var.environment != "prod"
  one_nat_gateway_per_az = var.environment == "prod"

  enable_dns_hostnames = true
  enable_dns_support   = true

  # VPC Flow Log — 항상 활성화(감사/보안)
  enable_flow_log                                 = true
  create_flow_log_cloudwatch_log_group            = true
  create_flow_log_cloudwatch_iam_role             = true
  flow_log_max_aggregation_interval               = 60
  flow_log_cloudwatch_log_group_retention_in_days = var.environment == "prod" ? 90 : 30

  # EKS Fargate 전용 subnet 태그 — ALB Controller가 자동 발견
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = "1"
    "karpenter.sh/discovery"          = "${var.project}-${var.environment}"
  }
  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }

  # 별도 DB subnet group 생성 (Aurora 모듈이 사용)
  create_database_subnet_group           = true
  create_database_subnet_route_table     = true
  create_database_internet_gateway_route = false # DB는 외부 접근 불가
  create_database_nat_gateway_route      = false

  # ElastiCache subnet group
  create_elasticache_subnet_group = true

  tags = merge(var.tags, {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "vpc"
  })
}
