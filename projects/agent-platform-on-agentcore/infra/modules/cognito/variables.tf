variable "project" {
  type = string
}

variable "admin_email" {
  type = string
}

variable "admin_password" {
  type      = string
  sensitive = true
}

variable "user_email" {
  type = string
}

variable "user_password" {
  type      = string
  sensitive = true
}

# Filling these turns the app client into an Authorization Code + PKCE (hosted
# UI) client and creates the hosted UI domain. Empty leaves the pool exactly as
# before: password auth only, no domain.
variable "callback_urls" {
  description = "Hosted UI OAuth callback URLs. Empty = no OAuth (password-auth only)."
  type        = list(string)
  default     = []
}

variable "logout_urls" {
  description = "Hosted UI sign-out URLs."
  type        = list(string)
  default     = []
}

variable "domain_prefix" {
  description = "Globally-unique Cognito hosted UI domain prefix. Empty = no domain."
  type        = string
  default     = ""
}
