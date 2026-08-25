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

# ==============================================================================
# VPC Endpoints — Bedrock PrivateLink (NAT 미경유, VPC 내부 직접 연결)
# ------------------------------------------------------------------------------
# Pod → ENI (VPC Endpoint) → Bedrock. 퍼블릭 인터넷을 경유하지 않음.
# private_dns_enabled=true: 기존 bedrock-runtime.{region}.amazonaws.com 호출이
# 코드 변경 없이 자동으로 VPC Endpoint 프라이빗 IP로 해석됨.
# ==============================================================================

resource "aws_security_group" "vpce_bedrock" {
  name_prefix = "${var.project}-${var.environment}-vpce-bedrock-"
  vpc_id      = module.vpc.vpc_id
  description = "Allow HTTPS from private subnets to Bedrock VPC Endpoint"

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
    description = "HTTPS from Fargate pods"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-vpce-bedrock"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_endpoint" "bedrock_runtime" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpce_bedrock.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-vpce-bedrock-runtime"
  })
}

resource "aws_vpc_endpoint" "bedrock" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.bedrock"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpce_bedrock.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-vpce-bedrock"
  })
}

resource "aws_vpc_endpoint" "sts" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.sts"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnets
  security_group_ids  = [aws_security_group.vpce_bedrock.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-vpce-sts"
  })
}
