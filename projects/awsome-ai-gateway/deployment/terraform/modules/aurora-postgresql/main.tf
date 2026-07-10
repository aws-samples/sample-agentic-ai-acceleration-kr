# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# Aurora PostgreSQL — terraform-aws-modules/rds-aurora/aws wrapper
# ------------------------------------------------------------------------------
# - dev: Serverless v2 (비용 절감, 0.5~2 ACU)
# - prod: Provisioned Multi-AZ (HA, 고정 성능)
# - 비밀번호는 Secrets Manager에 자동 저장
# - Performance Insights / Enhanced Monitoring 자동 활성화
# ==============================================================================

locals {
  is_prod = var.environment == "prod"

  # prod는 provisioned 클래스, dev는 Serverless v2
  cluster_instance_class = local.is_prod ? var.prod_instance_class : "db.serverless"

  # prod는 최소 2 인스턴스 (writer + reader), dev는 1 인스턴스
  instance_count = local.is_prod ? 2 : 1
}

module "aurora" {
  source  = "terraform-aws-modules/rds-aurora/aws"
  version = "~> 9.10"

  name           = "${var.project}-${var.environment}"
  engine         = "aurora-postgresql"
  engine_version = var.engine_version
  engine_mode    = "provisioned" # Serverless v2도 engine_mode=provisioned
  database_name  = var.database_name

  # Serverless v2 capacity (dev용 — prod는 빈 맵으로 전달).
  # 하위 module `terraform-aws-modules/rds-aurora` 가 `length()` 로 이 값을 검사하는데
  # null 이면 "argument must not be null" 에러. 빈 map 을 넘기면 serverless 모드 비활성.
  serverlessv2_scaling_configuration = local.is_prod ? {} : {
    min_capacity = 0.5
    max_capacity = 4.0
  }

  # 인스턴스 정의
  instances = {
    for i in range(local.instance_count) : "instance-${i + 1}" => {
      instance_class = local.cluster_instance_class
    }
  }

  vpc_id                 = var.vpc_id
  db_subnet_group_name   = var.db_subnet_group_name
  create_db_subnet_group = false

  # 보안 그룹 — EKS private subnet 으로부터만 허용
  security_group_rules = {
    eks_ingress = {
      description = "Aurora from EKS Fargate private subnets"
      cidr_blocks = var.private_subnet_cidrs
    }
  }

  # 인증 — admin 유저는 Secrets Manager 자동 관리
  master_username               = var.master_username
  manage_master_user_password   = true # Secrets Manager 자동 로테이션 지원
  master_user_secret_kms_key_id = var.kms_key_id

  # 가용성
  # availability_zones 는 넘기지 않음 (null 로 두면 AWS 가 DBSubnetGroup 에서 자동 선택).
  # 명시하면 subnet group AZ 와 불일치할 때 cluster replace 가 트리거됨.
  # availability_zones       = var.availability_zones
  backup_retention_period   = local.is_prod ? 14 : 7
  preferred_backup_window   = "17:00-19:00" # KST 02:00-04:00 (UTC 17-19)
  deletion_protection       = local.is_prod
  skip_final_snapshot       = !local.is_prod
  final_snapshot_identifier = local.is_prod ? "${var.project}-${var.environment}-final-${formatdate("YYYYMMDDHHmmss", timestamp())}" : null

  # 성능 관측성
  performance_insights_enabled          = true
  performance_insights_retention_period = local.is_prod ? 62 : 7
  monitoring_interval                   = 60
  create_monitoring_role                = true

  # 로그 CloudWatch export
  enabled_cloudwatch_logs_exports = ["postgresql"]

  # 스토리지 암호화
  storage_encrypted = true
  kms_key_id        = var.kms_key_id

  # Parameter Group — 커스텀 파라미터 (pgaudit 등)
  create_db_cluster_parameter_group      = true
  db_cluster_parameter_group_family      = "aurora-postgresql${split(".", var.engine_version)[0]}"
  db_cluster_parameter_group_name        = "${var.project}-${var.environment}-cluster-pg"
  db_cluster_parameter_group_description = "Cluster PG for ${var.project}-${var.environment}"
  db_cluster_parameter_group_parameters = [
    {
      name         = "log_statement"
      value        = local.is_prod ? "ddl" : "all"
      apply_method = "pending-reboot"
    },
    {
      name         = "log_min_duration_statement"
      value        = "1000" # 1초 이상 쿼리 로그
      apply_method = "immediate"
    },
    {
      name         = "shared_preload_libraries"
      value        = "pg_stat_statements"
      apply_method = "pending-reboot"
    },
  ]

  create_db_parameter_group      = true
  db_parameter_group_family      = "aurora-postgresql${split(".", var.engine_version)[0]}"
  db_parameter_group_name        = "${var.project}-${var.environment}-instance-pg"
  db_parameter_group_description = "Instance PG for ${var.project}-${var.environment}"

  # 자동 마이너 버전 업그레이드 (보안 패치)
  auto_minor_version_upgrade = true

  # 복사된 태그
  copy_tags_to_snapshot = true

  tags = merge(var.tags, {
    Project     = var.project
    Environment = var.environment
    Module      = "aurora-postgresql"
  })
}
