# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# External Secrets Operator 설치
# ------------------------------------------------------------------------------
# - Helm chart 로 ESO 설치 (CRD 포함)
# - ClusterSecretStore 생성은 별도 단계 (install-eks.sh 의 kubectl apply) 에서 처리.
#   Helm 이 CRD 등록과 CR 생성을 같은 apply 로 시도해 실패하는 문제 회피.
# ==============================================================================

locals {
  # chart major 버전(형식 검증은 variables.tf 의 chart_version validation).
  # 아래 1.x/2.x 전용 설정은 major >= 1 일 때만 넣는다. chart_version 을 0.10.x 로 고정한
  # 클러스터는 이 설정들이 생기기 전과 같은 values 로 렌더되므로, 그 상태로 레포를 갱신해도
  # plan 에 helm_release 변경이 생기지 않는다(webhook·cert-controller 도 그대로 남는다).
  chart_major = tonumber(regex("^v?([0-9]+)\\.", var.chart_version)[0])

  # webhook·cert-controller 비활성 + CRD 서빙 플래그. 근거는 아래 resource 의 주석.
  v1_only_settings = local.chart_major >= 1 ? [
    { name = "webhook.create", value = "false" },
    { name = "certController.create", value = "false" },
    { name = "crds.conversion.enabled", value = "false" },
    { name = "crds.unsafeServeV1Beta1", value = var.serve_v1beta1 ? "true" : "false" },
  ] : []
}

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

  # --------------------------------------------------------------------------
  # chart major >= 1 에서만: webhook·cert-controller 비활성 + CRD 서빙 플래그
  # (local.v1_only_settings, 아래 dynamic "set"). 0.10.x 에서는 아무것도 넣지 않는다.
  #
  # webhook·cert-controller 를 끄는 이유 — Fargate 에서 ESO webhook 의 서빙 인증서 SAN 이
  # 서비스 DNS 가 아니라 노드 호스트명으로 나가는 일이 반복된다(troubleshooting.md
  # "ESO webhook TLS SAN 불일치"). 파드는 1/1 Ready 이고 VWC 도 있어 겉으로는 멀쩡한데,
  # 실제 admission 요청은 전부 TLS 검증에 실패한다:
  #
  #   Error from server (InternalError): failed calling webhook
  #   "validate.externalsecret.external-secrets.io": tls: failed to verify certificate:
  #   x509: certificate is valid for fargate-ip-<node-ip>..., not
  #   external-secrets-webhook.external-secrets.svc
  #
  # `externalsecret-validate` 의 `failurePolicy` 는 **Fail** 이라 결과는 ExternalSecret
  # 생성·수정 전면 차단이다(기존 객체의 reconcile 은 admission 을 타지 않아 SecretSynced 로
  # 정상이라 증상이 드러나지 않는다). 그러면 llm-gateway 차트의 ExternalSecret
  # `pre-upgrade` 훅이 admission 에서 막혀 **`helm upgrade` 자체가 불가능**하다.
  # 파드 Ready 는 인증서 유효성을 증명하지 않는다 — webhook 건강은 실제 admission 요청으로만
  # 판정할 수 있다.
  #
  # install-eks.sh 가 설치 전에 VWC 를 지우는 것도 같은 이유다. 웹훅 없는 cert-controller 는
  # 재시작 시 readiness 교착(0/1)만 남기므로 함께 끈다. 검증 실패는 admission 거절 대신
  # reconcile 의 SecretSyncedError 로 드러난다.
  # 되돌리려면 먼저 인증서 문제를 고쳐야 하고, 그건 별개 작업이다.
  #
  # crds.conversion.enabled=false — chart README 는 webhook 을 끄면 이 플래그도 끄라고 한다
  # (안 끄면 kube-apiserver 가 없는 webhook 으로 conversion 을 시도한다). chart 2.x 는 이
  # 플래그와 무관하게 ExternalSecret CRD 에 `conversion` 블록을 만들지 않는다(true 로 렌더해도
  # 전략이 `None`). 0.10.x 에서 올라온 클러스터의 `Webhook` → `None` 전환은 플래그로 막을 수
  # 없으므로, storedVersions 에 v1alpha1 이 없는지를 올리기 전에 확인한다(09 런북 C1).
  #
  # crds.unsafeServeV1Beta1 — v1beta1 → v1 전환 창. 상세 근거는 variables.tf 의 serve_v1beta1.
  #
  # ⚠️ chart 를 0.10.x 에서 1.x 이상으로 올리는 apply 에서 webhook·cert-controller
  #    Deployment 와 VWC 가 제거된다(의도한 변화).
  dynamic "set" {
    for_each = local.v1_only_settings
    content {
      name  = set.value.name
      value = set.value.value
    }
  }

  atomic          = true
  cleanup_on_fail = true
  timeout         = 600
  wait            = true
}
