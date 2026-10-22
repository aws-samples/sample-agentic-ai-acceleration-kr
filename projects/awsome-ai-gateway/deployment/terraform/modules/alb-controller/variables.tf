# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "cluster_name" {
  description = "EKS cluster 이름"
  type        = string
}

variable "oidc_provider_arn" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "chart_version" {
  description = "aws-load-balancer-controller Helm chart 버전. https://github.com/aws/eks-charts/tree/master/stable/aws-load-balancer-controller"
  type        = string
  # 1.8.2(app v2.8.2) → 3.5.0(app v3.5.0).
  # ⚠️ v3.0.0 부터 **chart 버전 = app 버전**이다. v2.x 는 chart 1.x 였다
  #    (LBC v2.17 = chart 1.17). 그래서 1.8.2 → 3.5.0 은 두 자리 점프처럼 보이지만
  #    app 으로는 v2.8.2 → v3.5.0 이다.
  # ⚠️ helm 은 chart 의 crds/ 를 **업그레이드하지 않는다**(신규 설치 때만 만든다).
  #    이미 1.8.2 가 깔린 클러스터를 올릴 때는 v3 CRD 를 먼저 수동 apply 해야 한다:
  #      kubectl apply --server-side --force-conflicts -f <chart>/crds/crds.yaml
  #    (Gateway API 를 쓰지 않으므로 crds/gateway-crds.yaml 은 불필요.)
  #    이 모듈이 쓰는 두 CRD 의 버전 구조는 v2.8.2 와 **동일**하다
  #    (TargetGroupBinding v1alpha1+v1beta1(storage), IngressClassParams v1beta1(storage)),
  #    즉 저장버전 마이그레이션이 없고 CRD apply 는 신규 kind 2개
  #    (ALBTargetControlConfig, GlobalAccelerator) 추가뿐인 가산적 변경이다.
  #    절차: docs/eks-fargate/09-upgrade-131-to-136.md B 단계
  # ⚠️ 옛 기본값(1.8.2)으로 만든 클러스터는 환경 루트의 alb_controller_chart_version 으로
  #    라이브 값을 고정해 둔다. main.tf 의 set 키는 1.8.2·3.5.0 values.yaml 양쪽에 모두 있어
  #    (리네임 없음) 고정된 동안에는 이전과 같은 values 로 렌더된다.
  # 3.6.0(2026-10-05) 대신 3.5.0 을 기본값으로 두는 이유는 릴리스 후 기간이다.
  # v3.0.0 의 webhook 인증서 버그는 `enableCertManager=true` 사용자만 해당하고, 이 모듈은
  # 그 값을 설정하지 않는다(기본 false).
  default = "3.5.0"
}

variable "tags" {
  type    = map(string)
  default = {}
}
