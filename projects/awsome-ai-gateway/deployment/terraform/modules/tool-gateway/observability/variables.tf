variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "gateway_id" {
  type        = string
  description = "AgentCore Gateway ID"
}

variable "gateway_arn" {
  type        = string
  description = "AgentCore Gateway ARN"
}

variable "trace_sampling_percentage" {
  type        = number
  description = <<-EOT
    X-Ray Transaction Search indexing sampling percentage (0-100). This is the
    fraction of gateway traces indexed for querying. NOTE: the X-Ray "Default"
    indexing rule is ACCOUNT + REGION GLOBAL — setting it here affects every
    trace-producing resource in this region, not just this gateway. Use 100 in
    dev (low traffic, full visibility) and a low value (e.g. 10) in prod where
    error/latency visibility should come from metrics + alarms instead. Errors
    are always 100% in the vended logs regardless of this setting.
  EOT
  default     = 100

  validation {
    condition     = var.trace_sampling_percentage >= 0 && var.trace_sampling_percentage <= 100
    error_message = "trace_sampling_percentage must be between 0 and 100."
  }
}

variable "manage_trace_indexing_rule" {
  type        = bool
  description = <<-EOT
    Whether this stack manages the account/region-global X-Ray "Default"
    indexing rule. Only ONE Terraform stack per account+region may own it (it is
    a singleton). Set false in secondary stacks/gateways sharing the region to
    avoid fighting over the same global resource.
  EOT
  default     = true
}

variable "enable_otlp_export" {
  type        = bool
  description = "Whether to export OTLP spans/metrics to external backend"
  default     = false
}

variable "otlp_endpoint" {
  type        = string
  description = "OTLP collector endpoint (e.g., http://localhost:4317)"
  default     = ""
}
