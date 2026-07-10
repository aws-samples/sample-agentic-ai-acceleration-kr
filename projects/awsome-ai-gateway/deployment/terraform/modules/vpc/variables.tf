# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

variable "project" {
  description = "프로젝트 식별자 (예: llm-gateway)"
  type        = string
}

variable "environment" {
  description = "환경 (dev | staging | prod)"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod"
  }
}

variable "cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.0.0.0/16"
}

variable "azs" {
  description = "가용 영역 목록 (최소 2개, prod는 3개 권장)"
  type        = list(string)
}

variable "private_subnet_cidrs" {
  description = "워크로드용 private subnet CIDR 리스트 (Fargate Pod 배치)"
  type        = list(string)
}

variable "public_subnet_cidrs" {
  description = "ALB용 public subnet CIDR 리스트"
  type        = list(string)
}

variable "database_subnet_cidrs" {
  description = "Aurora 전용 격리 subnet CIDR 리스트"
  type        = list(string)
}

variable "elasticache_subnet_cidrs" {
  description = "ElastiCache 전용 격리 subnet CIDR 리스트"
  type        = list(string)
}

variable "tags" {
  description = "공통 태그"
  type        = map(string)
  default     = {}
}
