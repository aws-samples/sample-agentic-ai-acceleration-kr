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

  atomic          = true
  cleanup_on_fail = true
  timeout         = 600
  wait            = true
}
