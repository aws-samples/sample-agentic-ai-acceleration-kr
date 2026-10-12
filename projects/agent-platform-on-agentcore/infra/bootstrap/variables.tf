variable "project" {
  description = "Project name prefix"
  type        = string
  default     = "bap"
}

variable "cost_center" {
  description = "CostCenter tag applied to every resource for billing allocation."
  type        = string
  default     = "bap"
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "ap-northeast-1"
}
