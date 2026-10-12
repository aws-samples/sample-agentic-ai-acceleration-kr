variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "skills_bucket_arn" {
  type = string
}

variable "allowed_model_arns" {
  description = "Resources for bedrock:InvokeModel*. [\"*\"] = any model."
  type        = list(string)
  default     = ["*"]
}

variable "create_boundary" {
  description = "Also publish this role's policy document as a managed policy `$${project}-harness-boundary`. The server attaches it as the permissions boundary of every team role it creates, so a server-made role can never exceed what terraform grants a harness. The server narrows models per team inside that boundary."
  type        = bool
  default     = false
}
