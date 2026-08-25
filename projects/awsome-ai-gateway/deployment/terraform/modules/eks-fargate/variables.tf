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
  default     = "1.29"
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
