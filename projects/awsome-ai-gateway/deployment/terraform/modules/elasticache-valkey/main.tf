# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# ElastiCache for Valkey — Redis-compatible cluster
# ------------------------------------------------------------------------------
# - dev: 단일 노드 (cache.t4g.small)
# - prod: Cluster mode enabled, 3 shards × 2 nodes (writer+reader)
# - AUTH 토큰은 Secrets Manager로 관리
# - In-transit + At-rest 암호화 필수
# ==============================================================================

locals {
  is_prod = var.environment == "prod"

  # dev(비-클러스터)에서 replica 가 있으면(노드>1) failover/Multi-AZ 가 성립한다.
  # prod 는 cluster 모드라 항상 failover/Multi-AZ ON. 따라서 가용성 플래그는
  # "prod 이거나, dev 라도 노드가 2개 이상" 일 때 켠다(환경 이름 아닌 토폴로지 기준).
  dev_has_replica = !local.is_prod && var.dev_num_cache_clusters > 1
  ha_enabled      = local.is_prod || local.dev_has_replica

  # 파라미터그룹 선택(deepdive Q50 Phase4):
  #  - prod + 커스텀활성 → 커스텀 cluster 그룹(maxmemory-policy/reserved-memory)
  #  - prod + 기본       → AWS default.valkey7.cluster.on (기존 동작)
  #  - dev               → dev custom standalone 그룹
  prod_param_group = var.prod_enable_custom_param_group ? aws_elasticache_parameter_group.prod_cluster[0].name : "default.valkey7.cluster.on"
  parameter_group  = local.is_prod ? local.prod_param_group : aws_elasticache_parameter_group.this[0].name
}

# ------------------------------------------------------------------------------
# AUTH 토큰 — Secrets Manager에 저장
# ------------------------------------------------------------------------------
resource "random_password" "auth_token" {
  length  = 64
  special = false # ElastiCache AUTH token은 특수문자 제한 있음
}

resource "aws_secretsmanager_secret" "auth_token" {
  name        = "/${var.project}/${var.environment}/redis/auth_token"
  description = "ElastiCache Valkey AUTH token"
  kms_key_id  = var.kms_key_arn
  # destroy 시 즉시 삭제 (기본 30일 recovery window 가 재배포 시 name 충돌 유발).
  recovery_window_in_days = 0

  tags = merge(var.tags, {
    Module = "elasticache-valkey"
  })
}

resource "aws_secretsmanager_secret_version" "auth_token" {
  secret_id     = aws_secretsmanager_secret.auth_token.id
  secret_string = random_password.auth_token.result
}

# ------------------------------------------------------------------------------
# Security Group — EKS private subnet에서만 접근 허용
# ------------------------------------------------------------------------------
resource "aws_security_group" "this" {
  name        = "${var.project}-${var.environment}-elasticache"
  description = "ElastiCache Valkey - access from EKS"
  vpc_id      = var.vpc_id

  ingress {
    description = "Redis from EKS private subnet"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-elasticache"
  })
}

# ------------------------------------------------------------------------------
# Parameter Group
# ------------------------------------------------------------------------------
# AWS 특수 규칙: ElastiCache 는 parameter group "이름"이 `.cluster.on` 으로 끝나야
# cluster-enabled 로 인식 (family 는 `valkey7` 로 standalone/cluster 공통). 이 명명
# 규칙은 AWS 제공 default parameter group 에만 적용되고, 고객이 만든 custom group
# 은 항상 cluster-disabled 로 해석됨. 따라서:
#   - dev (standalone): custom parameter group 가능 → maxmemory-policy 커스터마이징
#   - prod (cluster mode): AWS default `default.valkey7.cluster.on` 사용 강제
#     (maxmemory-policy 등 커스터마이징 필요 시 default group 자체를 수정해야 하며,
#      이는 동일 account 의 모든 ElastiCache 에 영향 — 보통 권장되지 않음)
resource "aws_elasticache_parameter_group" "this" {
  count       = local.is_prod ? 0 : 1
  name        = "${var.project}-${var.environment}-valkey"
  family      = "valkey7"
  description = "Valkey parameters for ${var.project}-${var.environment}"

  # Lua 스크립트 캐시 확보 + eviction 전략
  parameter {
    name  = "maxmemory-policy"
    value = "volatile-lru"
  }

  tags = var.tags
}

# prod 커스텀 cluster-enabled 파라미터그룹(deepdive Q50 Phase4) — 기본 비활성.
# family `valkey7` 의 custom 그룹은 standalone 으로 해석되는 게 AWS 기본이나,
# `cluster-enabled=yes` 파라미터를 명시하면 cluster-mode 와 호환된다. 활성 시
# prod 가 default.valkey7.cluster.on(커스터마이징 불가) 대신 이 그룹을 써
# maxmemory-policy + reserved-memory-percent 를 박는다(메모리압박 시 noeviction
# OOM-거부 회피 + failover/replication/BGSAVE 헤드룸).
resource "aws_elasticache_parameter_group" "prod_cluster" {
  count       = local.is_prod && var.prod_enable_custom_param_group ? 1 : 0
  name        = "${var.project}-${var.environment}-valkey-cluster"
  family      = "valkey7"
  description = "Valkey cluster params for ${var.project}-${var.environment} (custom)"

  parameter {
    name  = "cluster-enabled"
    value = "yes"
  }
  parameter {
    name  = "maxmemory-policy"
    value = var.prod_maxmemory_policy
  }
  parameter {
    name  = "reserved-memory-percent"
    value = tostring(var.prod_reserved_memory_percent)
  }

  # NOTE(deepdive Q50 Phase4): `maxclients` 는 ElastiCache 에서 **수정 불가**
  # (노드 타입별 AWS 고정값, 파라미터그룹에 넣으면 apply 거부). 따라서 커넥션
  # 상한은 **클라이언트 풀 크기(values redis.poolSize)** 로만 통제한다 — pod 당
  # poolSize × HPA maxReplicas ≤ 노드 maxclients 합 을 지키는 게 유일한 가드.

  tags = var.tags
}

# ------------------------------------------------------------------------------
# Replication Group
# ------------------------------------------------------------------------------
resource "aws_elasticache_replication_group" "this" {
  replication_group_id = "${var.project}-${var.environment}"
  description          = "LLM Gateway Valkey cache - ${var.environment}"

  engine         = "valkey"
  engine_version = var.engine_version
  node_type      = local.is_prod ? var.prod_node_type : "cache.t4g.small"
  port           = 6379

  # Cluster mode — prod만. 샤드당 replica 는 var.prod_replicas_per_node_group(기본1,
  # 2 권장: failover zero-redundancy 윈도우 제거 + read 스케일 — deepdive Q50 Phase4-f).
  num_node_groups         = local.is_prod ? 3 : null
  replicas_per_node_group = local.is_prod ? var.prod_replicas_per_node_group : null

  # non-cluster mode — dev. 기본 1 = 단일 노드(replica·failover·Multi-AZ 없음).
  # var.dev_num_cache_clusters 를 2+ 로 올리면 primary + (n-1) replica 가 되고
  # 아래 가용성 플래그(local.ha_enabled)가 자동으로 failover/Multi-AZ 를 켠다.
  num_cache_clusters = local.is_prod ? null : var.dev_num_cache_clusters

  # 파라미터그룹: local.parameter_group 가 prod-custom/prod-default/dev 를 선택(상단 locals).
  parameter_group_name = local.parameter_group
  subnet_group_name    = var.subnet_group_name
  security_group_ids   = [aws_security_group.this.id]

  # 암호화
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.auth_token.result
  auth_token_update_strategy = "ROTATE"
  kms_key_id                 = var.kms_key_arn

  # 가용성 — prod(cluster) 항상 ON, dev 는 replica(노드>1)일 때만 ON.
  # 단일노드 dev 에서 이 둘을 켜면 apply 오류(replica 없음) → 토폴로지 기준 게이팅.
  automatic_failover_enabled = local.ha_enabled
  multi_az_enabled           = local.ha_enabled
  snapshot_retention_limit   = local.is_prod ? 7 : 1
  snapshot_window            = "17:00-19:00"         # KST 02-04
  maintenance_window         = "sun:20:00-sun:22:00" # KST 일 05-07
  auto_minor_version_upgrade = true

  # 로그
  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.slow.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }
  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.engine.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "engine-log"
  }

  apply_immediately = !local.is_prod

  tags = merge(var.tags, {
    Project     = var.project
    Environment = var.environment
  })
}

# EN: Slow/engine logs may carry pseudonymous IDs (Redis key names contain
#     UUID user_ids etc.). When `var.log_kms_key_arn` is null the log group
#     uses the AWS-owned key (default). Set the variable to a CMK ARN when
#     compliance requires customer-managed encryption — see variables.tf for
#     the required key policy grant.
# KO: 슬로우/엔진 로그에는 가명 식별자(예: Redis 키 이름의 user_id UUID)가
#     포함될 수 있습니다. `var.log_kms_key_arn` 이 null 이면 AWS 기본 키를
#     사용 (기본값). 컴플라이언스 요건이 고객-관리 암호화를 요구할 때
#     CMK ARN 을 주입합니다 — 필요한 키 정책 grant 는 variables.tf 참조.
resource "aws_cloudwatch_log_group" "slow" {
  name              = "/aws/elasticache/${var.project}-${var.environment}/slow"
  retention_in_days = local.is_prod ? 30 : 7
  kms_key_id        = var.log_kms_key_arn
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "engine" {
  name              = "/aws/elasticache/${var.project}-${var.environment}/engine"
  retention_in_days = local.is_prod ? 30 : 7
  kms_key_id        = var.log_kms_key_arn
  tags              = var.tags
}
