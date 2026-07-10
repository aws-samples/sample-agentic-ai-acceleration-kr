# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# RDS Proxy — Aurora 앞단 connection pool (선택)
# ------------------------------------------------------------------------------
# 사용 목적:
#   10,000명 / 4,000 동시 SSE / 500+ RPS 트래픽에서 EKS Pod 합산 연결이
#   Aurora max_connections(db.r7g.large ≈ 1,000) 한계를 상회할 수 있음.
#   Proxy 는 클라이언트-측 세션을 풀에 매핑하고 Aurora 실제 연결을 공유.
#
# 활성화 조건: var.enable_rds_proxy = true (기본 false)
#   애플리케이션 endpoint 를 Proxy endpoint 로 교체하면 끝 (코드 변경 없음).
#
# 주의 — PostgreSQL Pinning:
#   prepared statement / SET / LISTEN / temp table 사용 시 해당 세션이
#   Proxy 연결 하나에 pin 되어 pooling 효과가 줄어듦. SQLAlchemy asyncpg 는
#   기본적으로 prepared statements 를 쓰므로 연결 인자에
#   `statement_cache_size=0` 설정 권장 (FR-3.3 부하 테스트 전 확인 필요).
# ==============================================================================

locals {
  proxy_enabled = var.enable_rds_proxy
  proxy_name    = "${var.project}-${var.environment}"
}

# ------------------------------------------------------------------------------
# Proxy Security Group
#   Proxy 인바운드: EKS private subnet CIDR 에서 5432
#   Proxy 아웃바운드: Aurora cluster (별도 rule 불필요 — Aurora SG 에
#     ingress 로 Proxy SG 를 허용)
# ------------------------------------------------------------------------------
resource "aws_security_group" "proxy" {
  count       = local.proxy_enabled ? 1 : 0
  name        = "${local.proxy_name}-rds-proxy"
  description = "RDS Proxy SG for ${local.proxy_name}"
  vpc_id      = var.vpc_id

  # EKS private subnet 에서 들어오는 5432
  ingress {
    description = "PostgreSQL from EKS private subnets"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
  }

  # Aurora 로 나가는 연결 (VPC 내부 아무 곳 허용 — Aurora SG 가 최종 필터)
  egress {
    description = "Outbound to Aurora"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name        = "${local.proxy_name}-rds-proxy"
    Environment = var.environment
    Module      = "aurora-postgresql"
  })
}

# Aurora SG 에 Proxy SG 를 ingress 로 허용 (Proxy → Aurora 경로)
resource "aws_security_group_rule" "aurora_from_proxy" {
  count                    = local.proxy_enabled ? 1 : 0
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = module.aurora.security_group_id
  source_security_group_id = aws_security_group.proxy[0].id
  description              = "Allow RDS Proxy to Aurora"
}

# ------------------------------------------------------------------------------
# IAM Role — Proxy 가 Secrets Manager 에서 master user 비밀번호를 읽음
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "proxy_assume" {
  count = local.proxy_enabled ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "proxy" {
  count              = local.proxy_enabled ? 1 : 0
  name               = "${local.proxy_name}-rds-proxy"
  assume_role_policy = data.aws_iam_policy_document.proxy_assume[0].json

  tags = merge(var.tags, {
    Environment = var.environment
    Module      = "aurora-postgresql"
  })
}

data "aws_iam_policy_document" "proxy_secrets" {
  count = local.proxy_enabled ? 1 : 0

  statement {
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [
      module.aurora.cluster_master_user_secret[0].secret_arn,
      aws_secretsmanager_secret.gateway_user[0].arn,
    ]
  }

  # KMS key (Secrets Manager 암호화용) — null 이면 AWS managed key
  dynamic "statement" {
    for_each = var.kms_key_id == null ? [] : [1]
    content {
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [var.kms_key_id]
      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["secretsmanager.${data.aws_region.current[0].name}.amazonaws.com"]
      }
    }
  }
}

data "aws_region" "current" {
  count = local.proxy_enabled ? 1 : 0
}

resource "aws_iam_role_policy" "proxy_secrets" {
  count  = local.proxy_enabled ? 1 : 0
  name   = "${local.proxy_name}-rds-proxy-secrets"
  role   = aws_iam_role.proxy[0].id
  policy = data.aws_iam_policy_document.proxy_secrets[0].json
}

# ------------------------------------------------------------------------------
# Proxy 본체
# ------------------------------------------------------------------------------
resource "aws_db_proxy" "this" {
  count                  = local.proxy_enabled ? 1 : 0
  name                   = local.proxy_name
  engine_family          = "POSTGRESQL"
  role_arn               = aws_iam_role.proxy[0].arn
  vpc_subnet_ids         = var.proxy_private_subnet_ids
  vpc_security_group_ids = [aws_security_group.proxy[0].id]

  require_tls         = var.proxy_require_tls
  idle_client_timeout = var.proxy_idle_client_timeout

  # Aurora master user — init SQL / migration / Aurora admin 작업
  auth {
    auth_scheme = "SECRETS"
    secret_arn  = module.aurora.cluster_master_user_secret[0].secret_arn
    iam_auth    = "DISABLED"
    description = "Aurora master user via Secrets Manager"
  }

  # application gateway user — 모든 runtime Pod (gateway-proxy, admin-api, workers)
  auth {
    auth_scheme = "SECRETS"
    secret_arn  = aws_secretsmanager_secret.gateway_user[0].arn
    iam_auth    = "DISABLED"
    description = "Application gateway user via Secrets Manager"
  }

  tags = merge(var.tags, {
    Environment = var.environment
    Module      = "aurora-postgresql"
  })

  depends_on = [module.aurora, aws_secretsmanager_secret_version.gateway_user]
}

# ------------------------------------------------------------------------------
# Proxy default target group — Aurora cluster 를 target 으로 연결
# ------------------------------------------------------------------------------
resource "aws_db_proxy_default_target_group" "this" {
  count         = local.proxy_enabled ? 1 : 0
  db_proxy_name = aws_db_proxy.this[0].name

  connection_pool_config {
    max_connections_percent      = var.proxy_max_connections_percent
    max_idle_connections_percent = var.proxy_max_idle_connections_percent
    connection_borrow_timeout    = var.proxy_connection_borrow_timeout
  }
}

resource "aws_db_proxy_target" "this" {
  count                 = local.proxy_enabled ? 1 : 0
  db_proxy_name         = aws_db_proxy.this[0].name
  target_group_name     = aws_db_proxy_default_target_group.this[0].name
  db_cluster_identifier = module.aurora.cluster_id
}
