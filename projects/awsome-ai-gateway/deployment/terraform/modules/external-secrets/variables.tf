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
  description = "external-secrets Helm chart 버전(정확한 버전, 예: \"2.11.0\"). major >= 1 이면 main.tf 의 1.x/2.x 전용 설정(webhook·cert-controller 비활성, CRD 서빙 플래그)이 함께 들어가고, 0.x 면 들어가지 않는다."
  type        = string
  # 0.10.4 → 2.11.0.
  # 2.12.0(2026-10-06)보다 릴리스 후 기간이 긴 2.11.0(2026-09-18)을 기본값으로 둔다 — LBC 를
  # 3.6.0 대신 3.5.0 으로 둔 것과 같은 기준. 이 모듈이 의존하는 부분은 두 버전이 같다
  # (unsafeServeV1Beta1 플래그 존재, crds.conversion.enabled=false 기본, installCRDs 키 유지,
  # ExternalSecret CRD 버전 레이아웃 동일).
  #
  # 메이저 2개를 건너뛴다(v1.0.0 2025-11-07 / v2.0.0 2026-02-06). 이 두 메이저 릴리스의
  # 노트에 적힌 breaking 항목은 v2.0.0 의 Alibaba·Device42 프로바이더 제거뿐이고(v1.0.0 은
  # 없음), 이 모듈은 aws-secrets-manager 만 쓰므로 무관하다.
  # 그렇다고 0.10.4 → 2.11.0 의 변화가 그것뿐인 것은 아니다. 그 사이 0.x 릴리스들에서 API
  # 버전이 바뀌었다(`v1alpha1` 제거, `v1` 도입, `v1beta1` 기본 비서빙). 이 부분은
  # docs/eks-fargate/09-upgrade-131-to-136.md C 단계가 다룬다.
  #
  # ⚠️ 2.x 는 `external-secrets.io/v1` 만 serve 한다(v1beta1 은 아래 serve_v1beta1 로만).
  #    llm-gateway 차트(`llm-gateway.externalSecretsApiVersion`)와 install-eks.sh 의
  #    ClusterSecretStore 는 클러스터가 v1 을 serve 하면 v1, 아니면 v1beta1 을 쓴다.
  # ⚠️ LBC 와 달리 ESO 는 CRD 가 `templates/crds/` 에 있어 **helm 이 직접 관리**한다
  #    (LBC 는 `crds/` 라 helm 이 업그레이드하지 않아 수동 apply 가 필요하다).
  #    즉 이 버전 업만으로 CRD 가 즉시 교체되고, `serve_v1beta1=false` 라면 그 순간
  #    v1beta1 serving 이 끊긴다 → 아래 변수 설명 참고.
  # ⚠️ 옛 기본값(0.10.4)으로 만든 클러스터는 환경 루트의 external_secrets_chart_version 으로
  #    라이브 값을 고정해 둔다. 고정된 동안 이 모듈은 이전과 같은 values 로 렌더한다
  #    (main.tf 의 locals). 올리는 절차: docs/eks-fargate/09-upgrade-131-to-136.md C 단계.
  default = "2.11.0"

  validation {
    condition     = can(regex("^v?[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.-]+)?(\\+[0-9A-Za-z.-]+)?$", var.chart_version))
    error_message = "chart_version 은 정확한 chart 버전 하나여야 한다(MAJOR.MINOR.PATCH — 앞의 v 와 -prerelease·+build 접미사는 허용, 예: \"2.11.0\", \"0.10.4\"). 모듈이 major 버전으로 설정을 고르므로 범위·제약식(\"^2.0\", \">=2.0.0\", \"2.11.0 || 0.10.4\"), 부분 버전(\"2.11\"), 앞뒤 공백, 빈 값은 받지 않는다."
  }
}

variable "serve_v1beta1" {
  description = <<-EOT
    chart 의 `crds.unsafeServeV1Beta1`. `external-secrets.io/v1beta1` 을 계속 serve 할지.
    chart major 가 0(0.10.x 등)이면 이 값은 무시된다(모듈이 설정 자체를 넣지 않는다).

    평소에는 false(기본)다. 0.10.x 처럼 v1 을 모르는 ESO 가 깔린 기존 클러스터를 2.x 로
    올리는 동안에만 true 로 두는 전환용 다리다. 대상 CRD 는 4종이다:
    externalsecrets · clusterexternalsecrets · secretstores · clustersecretstores
    (모두 `*.external-secrets.io`).
    - 0.10.x 의 CRD 는 v1 을 정의하지 않는다(ExternalSecret·SecretStore·ClusterSecretStore 는
      `v1alpha1`+`v1beta1`(storage), ClusterExternalSecret 은 `v1beta1` 만) —
      `status.storedVersions = ["v1beta1"]` 이다.
    - 2.x 는 4종 모두 `v1` 이 served+storage 이고 `v1beta1` 은 `deprecated: true` +
      `served: {{ .Values.crds.unsafeServeV1Beta1 }}` (기본 false).
    - llm-gateway 차트의 ExternalSecret 은 `helm.sh/hook: pre-install,pre-upgrade`
      (weight -30) 라 apiVersion 이 안 맞으면 `helm upgrade` 자체가 훅 단계에서 실패한다.
      현재 차트는 클러스터를 보고 v1 을 고르지만, v1beta1 로 렌더된 옛 revision 으로의
      `helm rollback` 이나 v1beta1 로 쓰인 외부 매니페스트는 이 창 동안에만 살아 있다.

    순서: true 로 업그레이드 → llm-gateway 를 v1 로 helm upgrade(6개 Deployment 롤링) →
    ESO 객체 전부 재기록 후 CRD 4종의 `status.storedVersions` 를 `["v1"]` 로 수렴 → false.
    객체가 0개인 kind 도 storedVersions 에는 v1beta1 이 남으므로 4종 모두 수렴시킨다.
    상세: docs/eks-fargate/09-upgrade-131-to-136.md C 단계

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
