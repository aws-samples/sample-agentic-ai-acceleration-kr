variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "skills_bucket_arn" {
  type = string
}

variable "role_suffix" {
  description = "Role name suffix: `$${project}-harness-$${role_suffix}`. The default keeps the original single role's name; team roles pass the team name."
  type        = string
  default     = "execution"
}

variable "allowed_model_arns" {
  description = "Resources for bedrock:InvokeModel*. [\"*\"] = any model. A team role narrows this to the models the team may run — the IAM hard limit behind the server's allow-list."
  type        = list(string)
  default     = ["*"]
}
