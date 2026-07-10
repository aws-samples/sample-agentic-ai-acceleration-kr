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
  default     = "1.8.2"
}

variable "tags" {
  type    = map(string)
  default = {}
}
