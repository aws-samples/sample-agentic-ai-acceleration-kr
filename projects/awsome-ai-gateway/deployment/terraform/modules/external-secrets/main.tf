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

  # --------------------------------------------------------------------------
  # webhook·cert-controller 비활성 (레퍼런스 dev·prod 와 같은 구성, dev 2026-10-22 실측).
  #
  # Fargate 에서 ESO webhook 의 서빙 인증서 SAN 이 서비스 DNS 가 아니라 노드 호스트명으로
  # 나가는 일이 반복된다(troubleshooting.md "ESO webhook TLS SAN 불일치"). 파드는 1/1 Ready 이고
  # VWC 도 있어 겉으로는 멀쩡한데, 실제 admission 요청은 전부 TLS 검증에 실패한다:
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
  # ⚠️ 이 블록 이전 버전으로 설치된 클러스터는 다음 apply 에서 webhook·cert-controller
  #    Deployment 와 VWC 가 제거된다(의도한 변화).
  set {
    name  = "webhook.create"
    value = "false"
  }
  set {
    name  = "certController.create"
    value = "false"
  }
  # chart 2.x 는 이 플래그와 무관하게 ExternalSecret CRD 에 `conversion` 블록을 아예
  # 만들지 않는다(실측: `--set crds.conversion.enabled=true` 로 렌더해도 전략이 `None`).
  # 0.10.x 에서 올라온 클러스터의 `Webhook` → `None` 전환은 플래그로 막을 수 없다.
  # storedVersions 에 v1alpha1 이 없는지는 올리기 전에 확인한다(09 런북).
  set {
    name  = "crds.conversion.enabled"
    value = "false"
  }

  # v1beta1 → v1 전환 창. 상세 근거는 variables.tf 의 serve_v1beta1 설명.
  set {
    name  = "crds.unsafeServeV1Beta1"
    value = var.serve_v1beta1 ? "true" : "false"
  }

  atomic          = true
  cleanup_on_fail = true
  timeout         = 600
  wait            = true
}
