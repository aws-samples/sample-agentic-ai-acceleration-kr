variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "tool_name" {
  type        = string
  description = "Name of the tool (e.g., serper, exa, duckduckgo)"
}

variable "source_root" {
  type        = string
  description = "Root directory where tool source code is located (e.g., /path/to/tools)"
}

variable "env_vars" {
  type        = map(string)
  description = "Environment variables to pass to the Lambda function"
  default     = {}
}

variable "timeout" {
  type        = number
  description = "Lambda function timeout in seconds"
  default     = 60
}

variable "memory_size" {
  type        = number
  description = "Lambda function memory size in MB"
  default     = 512
}

variable "log_retention_days" {
  type        = number
  description = "CloudWatch log retention in days"
  default     = 7
}

variable "pip_command" {
  type        = string
  description = "pip command to use for dependency installation"
  default     = "pip3"
}

# Packaging mode. "Zip" (default) builds a pip --target zip and uploads it
# directly — used by every search tool. "Image" builds a Docker image from
# <source_root>/<tool_name>/Dockerfile, pushes it to a per-tool ECR repo, and
# runs the Lambda from that image — used by the browser tool, whose
# browser-use + playwright dependency tree exceeds the 250 MB unzipped limit.
variable "package_type" {
  type        = string
  description = "Lambda packaging mode: \"Zip\" or \"Image\"."
  default     = "Zip"

  validation {
    condition     = contains(["Zip", "Image"], var.package_type)
    error_message = "package_type must be \"Zip\" or \"Image\"."
  }
}

# Lambda CPU architecture. Zip tools are built as arm64 (pip --platform). The
# Image path builds a Docker image for this arch; set x86_64 when the build host
# can't cross-compile/emulate arm64.
variable "architecture" {
  type        = string
  description = "Lambda architecture: \"arm64\" or \"x86_64\"."
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be \"arm64\" or \"x86_64\"."
  }
}

variable "browser_arn" {
  type        = string
  description = "If set, grants this Lambda permission to drive the given AgentCore browser and invoke Bedrock models (used only by the browser tool)."
  default     = ""
}

variable "enable_browser_policy" {
  type        = bool
  description = "Whether to attach the browser-session + Bedrock InvokeModel policy. Set true only for the browser tool. Gated as a plan-time-known bool (browser_arn is computed)."
  default     = false
}

variable "bedrock_model_arns" {
  type        = list(string)
  description = "Bedrock model/inference-profile ARNs the browser tool may invoke."
  default     = []
}

variable "vpc_config" {
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  description = "If set, attaches the Lambda to a VPC (used by the SearXNG tool to reach its internal ALB). null leaves the Lambda outside any VPC, unchanged."
  default     = null
}

variable "secret_arn" {
  type        = string
  description = "If set, grants this Lambda secretsmanager:GetSecretValue on this secret ARN (the tool's API key)."
  default     = ""
}

variable "enable_secret_policy" {
  type        = bool
  description = "Whether to attach the Secrets Manager GetSecretValue policy. Plan-time-known bool because secret_arn is a computed ARN. Set true only for tools that have a secret."
  default     = false
}
