variable "project" {
  type = string
}

variable "alert_email" {
  type    = string
  default = ""
  # Empty means no subscription is created. A fresh `terraform apply` must not
  # email anybody: an alarm that pages a stranger is worse than no alarm.
  description = "Address to subscribe to the alerts topic. Empty disables it."
}

variable "alb_arn_suffix" {
  type = string
}

variable "target_group_arn_suffix" {
  type = string
}

variable "usage_table_name" {
  type = string
}
