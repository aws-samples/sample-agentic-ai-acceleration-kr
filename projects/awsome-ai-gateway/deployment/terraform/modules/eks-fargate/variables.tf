# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "cluster_version" {
  description = "EKS Kubernetes 버전"
  type        = string
  # 기본값은 기존 배포를 흔들지 않도록 그대로 둔다. 신규 설치 권장 버전은
  # 환경별 terraform.tfvars.example 에 적혀 있다 (거기서 addon_versions 와 함께 지정).
  default = "1.29"
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  description = "Fargate Pod 배치용 private subnet IDs (AZ 3개 이상 권장)"
  type        = list(string)
}

variable "public_access_cidrs" {
  description = "dev에서 kubectl 접근 허용 CIDR"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "application_namespace" {
  description = "LLM Gateway가 설치될 네임스페이스"
  type        = string
  default     = "llm-gateway"
}

variable "addon_versions" {
  description = "EKS add-on 버전 (AWS 공식 호환성 표 확인: https://docs.aws.amazon.com/eks/latest/userguide/managing-add-ons.html)"
  type = object({
    coredns    = string
    kube_proxy = string
    vpc_cni    = string
  })
  # cluster_version 과 짝이 맞아야 한다 — 한 버전의 addon 은 다른 버전에 존재하지 않는다.
  # 호환값 조회:
  #   aws eks describe-addon-versions --addon-name coredns --kubernetes-version <ver> \
  #     --query 'addons[0].addonVersions[?compatibilities[0].defaultVersion==`true`].addonVersion'
  # 기본값은 기존 배포를 흔들지 않도록 그대로 둔다(cluster_version 기본값과 한 쌍).
  # 다른 버전으로 설치·업그레이드할 때 env 의 eks_addon_versions 로 pin 한다.
  # nullable=false 필수: env 가 null 을 넘겨도 기본값으로 폴백된다. 없으면 null 이 그대로
  # 들어와 var.addon_versions.coredns 참조가 plan 에서 터진다 (실제 겪음).
  nullable = false
  default = {
    coredns    = "v1.11.3-eksbuild.1"
    kube_proxy = "v1.29.7-eksbuild.2"
    vpc_cni    = "v1.18.3-eksbuild.1"
  }
}

variable "access_entries" {
  description = "EKS Access Entries — 관리자/CI 계정 → RBAC role 매핑"
  type        = any
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
