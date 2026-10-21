# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "irsa_role_arn" {
  description = "IRSA role ARN (irsa 모듈의 external_secrets_role_arn)"
  type        = string
}

variable "aws_region" {
  type = string
}

variable "chart_version" {
  description = "external-secrets Helm chart 버전"
  type        = string
  # 0.10.4 → 2.11.0. 레퍼런스 dev·prod 가 쓰는 값이다(dev 2026-10-22 실측).
  # 2.12.0(2026-10-06)이 최신이지만 1일차라 2.11.0(2026-09-18, 3주 숙성)을 쓴다
  # — LBC 를 3.6.0 대신 3.5.0 으로 고른 것과 같은 기준. 둘은 우리에게 중요한 부분이
  # 구조적으로 동일하다(실측: unsafeServeV1Beta1 플래그 존재, crds.conversion.enabled=false
  # 기본, installCRDs 키 생존, ExternalSecret CRD 버전 레이아웃 동일).
  #
  # 메이저 2개를 건너뛰지만(v1.0.0 2025-11-07 / v2.0.0 2026-02-06) 릴리스 노트상
  # breaking change 는 **v2.0.0 의 Alibaba·Device42 프로바이더 제거 단 하나**이고
  # 우리는 aws-secrets-manager 라 무관하다. v1.0.0 에는 breaking 항목이 없다.
  #
  # ⚠️ 2.x 는 `external-secrets.io/v1` 만 serve 한다(v1beta1 은 아래 serve_v1beta1 로만).
  #    llm-gateway 차트는 클러스터가 v1 을 serve 하면 v1 로 렌더하고
  #    (`llm-gateway.externalSecretsApiVersion`), install-eks.sh 의 ClusterSecretStore 도 v1 이다.
  # ⚠️ LBC 와 달리 ESO 는 CRD 가 `templates/crds/` 에 있어 **helm 이 직접 관리**한다
  #    (LBC 는 `crds/` 라 helm 이 업그레이드하지 않아 수동 apply 가 필요하다).
  #    즉 이 버전 업만으로 CRD 가 즉시 교체되고, `serve_v1beta1=false` 라면 그 순간
  #    v1beta1 serving 이 끊긴다 → 아래 변수 설명 참고.
  default = "2.11.0"
}

variable "serve_v1beta1" {
  description = <<-EOT
    chart 의 `crds.unsafeServeV1Beta1`. `external-secrets.io/v1beta1` 을 계속 serve 할지.

    평소에는 false(기본)다. 0.10.x 처럼 v1 을 모르는 ESO 가 깔린 기존 클러스터를 2.x 로
    올리는 동안에만 true 로 두는 전환용 다리다:
    - 0.10.x 의 CRD 는 `v1alpha1`+`v1beta1`(storage) 만 serve 하고
      `status.storedVersions = ["v1beta1"]` 이다.
    - 2.x 는 `v1` 이 served+storage 이고 `v1beta1` 은 `deprecated: true` +
      `served: {{ .Values.crds.unsafeServeV1Beta1 }}` (기본 false).
    - llm-gateway 차트의 ExternalSecret 은 `helm.sh/hook: pre-install,pre-upgrade`
      (weight -30) 라 apiVersion 이 안 맞으면 `helm upgrade` 자체가 훅 단계에서 실패한다.
      현재 차트는 클러스터를 보고 v1 을 고르지만, v1beta1 로 렌더된 옛 revision 으로의
      `helm rollback` 이나 v1beta1 로 쓰인 외부 매니페스트는 이 창 동안에만 살아 있다.

    순서: true 로 업그레이드 → llm-gateway 를 v1 로 helm upgrade(6개 Deployment 롤링) →
    ESO 객체 전부 재기록 후 CRD 3종 `status.storedVersions` 를 `["v1"]` 로 수렴 → false.
    상세: docs/eks-fargate/09-upgrade-131-to-136.md

    ⚠️ chart 주석에 "This flag will be removed on 2026.05.01" 이라고 적혀 있고 그 날짜는
       이미 지났다(2.11.0/2.12.0 에는 아직 존재). 상류가 지우면 이 다리는 사라지므로
       수렴 단계를 미루지 말 것.
    참고: `served: false` 는 `spec.versions` 에서 항목을 **지우는 것이 아니라서**
    storedVersions 가 v1beta1 을 담고 있어도 플래그를 끄는 것 자체는 허용된다.
    그래도 수렴시켜 두는 이유는 상류가 v1beta1 항목을 완전히 제거할 때 CRD 업데이트가
    거부되지 않게 하기 위해서다.
  EOT
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
