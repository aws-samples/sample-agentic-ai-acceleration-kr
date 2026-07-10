# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# AWS Load Balancer Controller — Fargate에서 ALB Ingress 동작에 필수
# ------------------------------------------------------------------------------
# Helm chart로 설치 + IRSA (ELB API 호출 권한)
# 공식 문서: https://kubernetes-sigs.github.io/aws-load-balancer-controller/
# ==============================================================================

# ------------------------------------------------------------------------------
# IRSA for ALB Controller
# ------------------------------------------------------------------------------
module "alb_controller_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.48"

  role_name                              = "${var.project}-${var.environment}-alb-controller"
  attach_load_balancer_controller_policy = true

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }

  tags = var.tags
}

# ------------------------------------------------------------------------------
# Helm release
# ------------------------------------------------------------------------------
resource "helm_release" "alb_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = var.chart_version
  namespace  = "kube-system"

  # values
  set {
    name  = "clusterName"
    value = var.cluster_name
  }

  set {
    name  = "serviceAccount.create"
    value = "true"
  }
  set {
    name  = "serviceAccount.name"
    value = "aws-load-balancer-controller"
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = module.alb_controller_irsa.iam_role_arn
  }

  set {
    name  = "region"
    value = var.aws_region
  }
  set {
    name  = "vpcId"
    value = var.vpc_id
  }

  # replica (prod HA)
  set {
    name  = "replicaCount"
    value = var.environment == "prod" ? "2" : "1"
  }

  # Service Mutator Webhook 비활성화.
  # - ALB Controller v2.4+ 는 모든 Service 생성 시 webhook 호출 (LoadBalancerClass 자동 부여용)
  # - 우리는 ALB Ingress 만 쓰고 type=LoadBalancer Service 를 쓰지 않으므로 불필요
  # - 더 중요: ALB Controller Pod 가 Fargate 에서 Ready 되기 전에 다른 namespace (ESO 등)
  #   에서 Service 를 만들면 webhook endpoint 없음 → ESO Helm install 실패.
  #   끄면 근본적으로 race 조건 차단.
  set {
    name  = "enableServiceMutatorWebhook"
    value = "false"
  }

  # Fargate Profile에 매칭되도록 namespace selector
  # (eks-fargate 모듈의 kube-system fargate profile이 이를 수용)

  atomic          = true
  cleanup_on_fail = true
  # Fargate 첫 Pod 프로비저닝(micro-VM + 이미지 pull)이 느려 10분 초과 사례 발생.
  # 15분으로 여유 — 재시도 불필요.
  timeout = 900
  # wait = true 기본이지만, Fargate Pod schedule 지연을 고려해 명시적으로 지정
  wait          = true
  wait_for_jobs = true

  depends_on = [module.alb_controller_irsa]
}
