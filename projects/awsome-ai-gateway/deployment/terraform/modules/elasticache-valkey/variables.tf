# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "engine_version" {
  description = "Valkey engine version"
  type        = string
  default     = "7.2"
}

variable "vpc_id" {
  type = string
}

variable "subnet_group_name" {
  description = "ElastiCache subnet group name (vpc 모듈의 elasticache_subnet_group_name)"
  type        = string
}

variable "private_subnet_cidrs" {
  description = "접근 허용 EKS subnet CIDR"
  type        = list(string)
}

variable "prod_node_type" {
  description = "Prod 노드 인스턴스 타입"
  type        = string
  default     = "cache.r7g.large"
}

# prod 샤드당 replica 수(deepdive Q50 Phase4-f). 기본 1 → primary failover 시 해당
# 샤드가 교체노드 프로비저닝까지 zero-redundancy. 2 로 올리면 그 윈도우 제거 + read
# 스케일 여유(노드 수 = 3샤드 × (1+replicas)). 비용↑(노드시간). 1=기존 동작.
variable "prod_replicas_per_node_group" {
  description = "prod cluster 샤드당 replica 수. 1=기존, 2=zero-redundancy 윈도우 제거"
  type        = number
  default     = 1

  validation {
    condition     = var.prod_replicas_per_node_group >= 1 && var.prod_replicas_per_node_group <= 5
    error_message = "prod_replicas_per_node_group 는 1~5 사이여야 합니다."
  }
}

# prod cluster 용 커스텀 파라미터(deepdive Q50 Phase4). 기본 비활성(AWS default
# `default.valkey7.cluster.on` 사용 — 기존 동작). 활성 시 모듈이 cluster-enabled
# 커스텀 파라미터그룹을 만들어 maxmemory-policy + reserved-memory-percent 를 박는다
# (prod 가 default 라 dev 의 volatile-lru 와 갈리고 메모리압박 시 noeviction → OOM
# 거부 위험을 닫음). family valkey7 의 cluster-enabled 커스텀 그룹은 동일 family 의
# cluster.on 파생이라 cluster-mode 와 호환된다.
variable "prod_enable_custom_param_group" {
  description = "prod 에 커스텀 cluster 파라미터그룹(maxmemory-policy/reserved-memory) 사용 여부"
  type        = bool
  default     = false
}

variable "prod_maxmemory_policy" {
  description = "prod 커스텀 파라미터그룹의 maxmemory-policy(prod_enable_custom_param_group=true 일 때)"
  type        = string
  default     = "volatile-lru"
}

variable "prod_reserved_memory_percent" {
  description = "prod 커스텀 파라미터그룹의 reserved-memory-percent(%). failover/replication/BGSAVE 헤드룸"
  type        = number
  default     = 25
}

# dev(비-클러스터) 노드 수. 기본 1 = 단일 노드(현재 동작, replica/failover 없음).
# 2 이상으로 올리면 primary + (n-1) replica 구성이 되고, 이 모듈은 자동으로
# automatic_failover + Multi-AZ 를 켠다(아래 main.tf locals). failover 동작을
# prod 전에 dev 에서 드릴/검증하고 싶을 때만 올린다(노드 수만큼 비용 증가).
# prod 는 항상 cluster 모드(num_node_groups)라 이 변수의 영향을 받지 않는다.
variable "dev_num_cache_clusters" {
  description = "dev(non-cluster) 노드 수. 1=단일노드(기본), 2+=primary+replica 로 failover/Multi-AZ 자동 활성화"
  type        = number
  default     = 1

  validation {
    condition     = var.dev_num_cache_clusters >= 1 && var.dev_num_cache_clusters <= 6
    error_message = "dev_num_cache_clusters 는 1~6 사이여야 합니다."
  }
}

variable "kms_key_arn" {
  description = "암호화용 KMS 키 ARN. null 이면 AWS 기본 키"
  type        = string
  default     = null
}

# CloudWatch Log Group 암호화 전용 KMS 키 ARN.
# EN: CloudWatch Logs only accepts a CMK whose key policy grants the
#     `logs.<region>.amazonaws.com` service principal `kms:Encrypt*`,
#     `kms:Decrypt*`, `kms:ReEncrypt*`, `kms:GenerateDataKey*`,
#     `kms:Describe*`. Without that grant the log group creation fails.
#     See: https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html
#     The ElastiCache slow/engine logs may include pseudonymous identifiers
#     (e.g. user_id UUIDs as Redis key names). They contain no direct PII
#     (no email/name/IP). Default `null` keeps the AWS-owned key (current
#     behaviour). Set this to a CMK ARN only when the deployment is subject
#     to compliance requirements that mandate customer-managed encryption
#     (e.g. ISMS-P, stricter GDPR profiles).
# KO: CloudWatch Logs 의 로그 그룹 암호화는 KMS 키 정책이
#     `logs.<region>.amazonaws.com` 서비스 주체에게 위 권한들을 grant 해야
#     합니다. 그렇지 않으면 로그 그룹 생성 자체가 실패합니다.
#     ElastiCache slow/engine 로그에는 user_id UUID 같은 가명 식별자가
#     Redis 키 이름의 일부로 포함될 수 있으나, 직접 PII (이메일/이름/IP)
#     는 포함되지 않습니다. 기본값 `null` 은 AWS 기본 키 사용 (현재 동작
#     유지). ISMS-P 등 컴플라이언스 요건이 고객-관리 암호화를 요구하는
#     경우에만 CMK ARN 을 주입하세요.
variable "log_kms_key_arn" {
  description = "Optional KMS CMK ARN for CloudWatch Log Group encryption. null = AWS-owned key (default). / CloudWatch Log Group 암호화용 KMS CMK ARN. null 이면 AWS 기본 키 사용 (기본값)."
  type        = string
  default     = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
