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
  default     = "0.10.4"
}

variable "tags" {
  type    = map(string)
  default = {}
}
