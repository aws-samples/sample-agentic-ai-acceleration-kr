# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# External Secrets Operator 설치
# ------------------------------------------------------------------------------
# - Helm chart 로 ESO 설치 (CRD 포함)
# - ClusterSecretStore 생성은 별도 단계 (install-eks.sh 의 kubectl apply) 에서 처리.
#   Helm 이 CRD 등록과 CR 생성을 같은 apply 로 시도해 실패하는 문제 회피.
# ==============================================================================

resource "helm_release" "external_secrets" {
  name             = "external-secrets"
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  version          = var.chart_version
  namespace        = "external-secrets"
  create_namespace = true

  set {
    name  = "installCRDs"
    value = "true"
  }
  set {
    name  = "serviceAccount.create"
    value = "true"
  }
  set {
    name  = "serviceAccount.name"
    value = "external-secrets"
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = var.irsa_role_arn
  }
  set {
    name  = "replicaCount"
    value = var.environment == "prod" ? "2" : "1"
  }

  # webhook·cert-controller 는 설치하지 않는다. 검증 웹훅(VWC)은 install-eks.sh 가
  # 설치 중 제거해 와서 실운영은 수개월간 무-VWC 로 돌았고, 웹훅이 없으면
  # cert-controller 는 readiness 교착(0/1)만 남긴다. 잘못된 ExternalSecret 은
  # admission 거절 대신 reconcile 의 SecretSyncedError 로 드러난다.
  set {
    name  = "webhook.create"
    value = "false"
  }
  set {
    name  = "certController.create"
    value = "false"
  }
  # CRD conversion 도 webhook 참조를 끊는다(전략 Webhook → None). 웹훅 없이
  # Webhook 전략이 남으면 CRD 변환 호출이 죽은 서비스를 가리킨다.
  set {
    name  = "crds.conversion.enabled"
    value = "false"
  }

  atomic          = true
  cleanup_on_fail = true
  timeout         = 600
  wait            = true
}
