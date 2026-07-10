# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# DB Secrets — Terraform-managed
# ------------------------------------------------------------------------------
# gateway user 와 관련 secret 을 Terraform 이 일괄 관리. operator 는 openssl 로
# 직접 password 를 생성하지 않고, Terraform 이 random_password 로 생성해 두
# 경로에 동일 값으로 박아둠:
#
#   /llm-gateway/<env>/db/gateway-user   (RDS Proxy auth 용 — {username, password})
#   /llm-gateway/<env>/db                (Helm ExternalSecret 용 — {password, master_password})
#
# 두 secret 의 `password` 값은 single source(random_password.gateway_user)에서
# 파생되므로 항상 동기화됨.
#
# 활성화 조건: var.enable_rds_proxy = true 일 때만 생성 (proxy auth 용).
#   Proxy 를 안 쓰는 경우엔 기존 operator 수동 생성 방식(03-secrets.md)을 사용.
# ==============================================================================

resource "random_password" "gateway_user" {
  count  = local.proxy_enabled ? 1 : 0
  length = 32
  # asyncpg URL encoding 복잡성 회피 — alphanumeric only
  special = false
  # 재생성 방지 (password rotation 필요 시 명시적 taint)
  lifecycle {
    ignore_changes = [length, special]
  }
}

# Aurora managed master secret 에서 password 를 꺼내 /db 의 master_password 키로 복사.
# 주의: Aurora 가 master password 를 rotate 하면 이 값은 apply 전까지 stale.
#       현재 manage_master_user_password=true 지만 자동 rotation 은 AWS default 가 아니므로
#       실질적 rotation 없음. 쓰는 경우엔 apply 또는 별도 sync 경로 필요.
data "aws_secretsmanager_secret_version" "aurora_master_current" {
  count     = local.proxy_enabled ? 1 : 0
  secret_id = module.aurora.cluster_master_user_secret[0].secret_arn
}

# ---- /db/gateway-user ---- (RDS Proxy auth format)
resource "aws_secretsmanager_secret" "gateway_user" {
  count       = local.proxy_enabled ? 1 : 0
  name        = "/${var.project}/${var.environment}/db/gateway-user"
  description = "Application 'gateway' user credentials — used by RDS Proxy auth."
  kms_key_id  = var.kms_key_id
  # destroy 시 즉시 삭제 (기본 30일 recovery window 가 재배포 시 name 충돌 유발).
  recovery_window_in_days = 0

  tags = merge(var.tags, {
    Environment = var.environment
    Module      = "aurora-postgresql"
    Purpose     = "rds-proxy-auth"
  })
}

resource "aws_secretsmanager_secret_version" "gateway_user" {
  count     = local.proxy_enabled ? 1 : 0
  secret_id = aws_secretsmanager_secret.gateway_user[0].id
  secret_string = jsonencode({
    username = "gateway"
    password = random_password.gateway_user[0].result
  })
}

# ---- /db ---- (Helm ExternalSecret format)
# Helm chart `database.external.passwordSecretName` 가 참조하는 기존 경로.
# 기존에 operator 가 수동 생성하던 것을 Terraform 으로 이관.
resource "aws_secretsmanager_secret" "db" {
  count       = local.proxy_enabled ? 1 : 0
  name        = "/${var.project}/${var.environment}/db"
  description = "DB credentials for Helm ExternalSecret (Terraform-managed)."
  kms_key_id  = var.kms_key_id
  # destroy 시 즉시 삭제 (기본 30일 recovery window 가 재배포 시 name 충돌 유발).
  recovery_window_in_days = 0

  tags = merge(var.tags, {
    Environment = var.environment
    Module      = "aurora-postgresql"
    Purpose     = "helm-external-secret"
  })
}

resource "aws_secretsmanager_secret_version" "db" {
  count     = local.proxy_enabled ? 1 : 0
  secret_id = aws_secretsmanager_secret.db[0].id
  secret_string = jsonencode({
    password        = random_password.gateway_user[0].result
    master_password = jsondecode(data.aws_secretsmanager_secret_version.aurora_master_current[0].secret_string).password
  })
}
