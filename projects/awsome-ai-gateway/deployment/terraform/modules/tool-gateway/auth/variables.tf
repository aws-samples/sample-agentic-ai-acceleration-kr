variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "cowork_redirect_uris" {
  type        = list(string)
  description = "Callback URLs for Cowork client (e.g., http://localhost:3000/callback)"
  default     = []
}

variable "external_oidc_issuer" {
  type        = string
  description = "External OIDC issuer URL (for external_oidc auth mode)"
  default     = ""
}

variable "external_oidc_audience" {
  type        = string
  description = "External OIDC audience"
  default     = ""
}

variable "auth_mode" {
  type        = string
  description = "Authentication mode: cognito or external_oidc"
  default     = "cognito"
  validation {
    condition     = contains(["cognito", "external_oidc"], var.auth_mode)
    error_message = "auth_mode must be either 'cognito' or 'external_oidc'"
  }
}
