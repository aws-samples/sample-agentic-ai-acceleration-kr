# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# EKS Fargate 클러스터 — terraform-aws-modules/eks/aws wrapper
# ------------------------------------------------------------------------------
# - 전 노드 Fargate (EC2 노드 그룹 없음)
# - Fargate Profile: kube-system (CoreDNS 등) + 애플리케이션 네임스페이스
# - OIDC Provider 자동 생성 → IRSA 가능
# - EKS add-on: CoreDNS, kube-proxy, VPC CNI (모두 Fargate 호환 버전)
# ==============================================================================

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.24"

  cluster_name    = "${var.project}-${var.environment}"
  cluster_version = var.cluster_version

  # OIDC provider 자동 생성 (IRSA 전제 조건)
  enable_irsa = true

  # Public endpoint: var.public_access_cidrs 가 비어있지 않으면 활성 (CIDR 화이트리스트).
  # 고객 인도 시 prod 에선 bastion/VPN 경유만 허용하려면 environments/prod/main.tf 에서
  # public_access_cidrs = [] 로 내려주면 자동으로 private-only 로 전환됨.
  cluster_endpoint_public_access       = length(var.public_access_cidrs) > 0
  cluster_endpoint_public_access_cidrs = var.public_access_cidrs
  cluster_endpoint_private_access      = true

  vpc_id     = var.vpc_id
  subnet_ids = var.private_subnet_ids

  # Fargate Profile 정의 — 네임스페이스별 매핑
  fargate_profiles = {
    # kube-system 전체 (CoreDNS, ALB Controller, 기타 애드온)
    # 특정 label 만 매칭하면 ALB Controller 같은 다른 kube-system Pod 가 Pending.
    kube-system = {
      name = "kube-system"
      selectors = [
        {
          namespace = "kube-system"
        }
      ]
      subnet_ids = var.private_subnet_ids
      tags       = var.tags
    }

    # 애플리케이션 네임스페이스
    application = {
      name = "application"
      selectors = [
        {
          namespace = var.application_namespace
        }
      ]
      subnet_ids = var.private_subnet_ids
      tags       = var.tags
    }

    # 운영/관측성 네임스페이스 (OTel/ESO 등)
    platform = {
      name = "platform"
      selectors = [
        {
          namespace = "external-secrets"
        },
        {
          namespace = "observability"
        }
      ]
      subnet_ids = var.private_subnet_ids
      tags       = var.tags
    }
  }

  # EKS add-on (Fargate 호환)
  # resolve_conflicts_on_update=OVERWRITE 필수.
  # 없으면 cluster_addons 의 configuration_values 변경이 실제 Deployment spec 에
  # 반영되지 않아, 예) `computeType=Fargate` 로 바꿔도 Deployment 에 toleration 이
  # 추가되지 않아 coredns Pod 가 Fargate 노드에 schedule 안 됨.
  cluster_addons = {
    coredns = {
      addon_version               = var.addon_versions.coredns
      resolve_conflicts_on_update = "OVERWRITE"
      resolve_conflicts_on_create = "OVERWRITE"
      configuration_values = jsonencode({
        computeType  = "Fargate"
        replicaCount = var.environment == "prod" ? 3 : 2

        # Fargate 노드의 taint 를 tolerate 하기 위해 명시적으로 지정해야 한다.
        # `computeType=Fargate` 만으로는 Deployment 에 toleration 이 자동 추가되지 않음
        # (AWS EKS addon 의 확인된 한계). 빠지면 Pod Pending 으로 schedule 실패.
        tolerations = [
          {
            key      = "CriticalAddonsOnly"
            operator = "Exists"
          },
          {
            key    = "node-role.kubernetes.io/control-plane"
            effect = "NoSchedule"
          },
          {
            key      = "eks.amazonaws.com/compute-type"
            operator = "Equal"
            value    = "fargate"
            effect   = "NoSchedule"
          }
        ]

        # CoreDNS는 Fargate Pod 최소 리소스 반영
        resources = {
          requests = {
            cpu    = "250m"
            memory = "512Mi"
          }
          limits = {
            cpu    = "500m"
            memory = "1Gi"
          }
        }
      })
    }
    kube-proxy = {
      addon_version               = var.addon_versions.kube_proxy
      resolve_conflicts_on_update = "OVERWRITE"
      resolve_conflicts_on_create = "OVERWRITE"
    }
    vpc-cni = {
      addon_version               = var.addon_versions.vpc_cni
      service_account_role_arn    = module.vpc_cni_irsa.iam_role_arn
      resolve_conflicts_on_update = "OVERWRITE"
      resolve_conflicts_on_create = "OVERWRITE"
    }
  }

  # 클러스터 보안 그룹 추가 규칙 — 기본값 사용
  # AWS Load Balancer Controller 가 target-type=ip 사용 시 ALB -> Pod ENI 접근을
  # 자동으로 SG 규칙에 추가하므로 수동 추가 불필요.
  # cluster_security_group_additional_rules = {}

  # 클러스터 로그 (감사/security)
  cluster_enabled_log_types = [
    "api",
    "audit",
    "authenticator",
    "controllerManager",
    "scheduler"
  ]
  cloudwatch_log_group_retention_in_days = var.environment == "prod" ? 90 : 30

  # KMS 암호화 (Secret at rest)
  cluster_encryption_config = {
    resources        = ["secrets"]
    provider_key_arn = aws_kms_key.eks.arn
  }

  # Access Entries — 관리자 접근
  access_entries = var.access_entries

  tags = merge(var.tags, {
    Project     = var.project
    Environment = var.environment
    Module      = "eks-fargate"
  })
}

# ------------------------------------------------------------------------------
# KMS key for EKS Secret 암호화
# ------------------------------------------------------------------------------
resource "aws_kms_key" "eks" {
  description             = "${var.project}-${var.environment} EKS Secret encryption"
  deletion_window_in_days = var.environment == "prod" ? 30 : 7
  enable_key_rotation     = true

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-eks"
  })
}

resource "aws_kms_alias" "eks" {
  name          = "alias/${var.project}-${var.environment}-eks"
  target_key_id = aws_kms_key.eks.id
}

# ------------------------------------------------------------------------------
# VPC CNI IRSA (애드온에 필요)
# ------------------------------------------------------------------------------
module "vpc_cni_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.48"

  role_name = "${var.project}-${var.environment}-vpc-cni"

  attach_vpc_cni_policy = true
  vpc_cni_enable_ipv4   = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-node"]
    }
  }

  tags = var.tags
}
