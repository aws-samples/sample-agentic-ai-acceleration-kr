variable "project" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
variable "alb_sg_id" { type = string }
variable "web_sg_id" { type = string }
variable "server_sg_id" { type = string }
variable "execution_role_arn" { type = string }
variable "task_role_arn" { type = string }

variable "server_image" { type = string }
variable "web_image" { type = string }

variable "server_env" {
  description = "Environment variables for server container"
  type        = map(string)
  default     = {}
}

variable "web_env" {
  description = "Environment variables for web container"
  type        = map(string)
  default     = {}
}

variable "server_cpu" {
  type    = number
  default = 512
}

variable "server_memory" {
  type    = number
  default = 1024
}

variable "web_cpu" {
  type    = number
  default = 256
}

variable "web_memory" {
  type    = number
  default = 512
}

variable "namespace_name" {
  type    = string
  default = "bap.local"
}

variable "desired_count" {
  description = "Tasks per service. 0 parks the environment without destroying it."
  type        = number
  default     = 1
}

# Whether to terminate TLS. Must be known at plan time (it drives listener
# count), which is why it is separate from certificate_arn — an ACM ARN created
# in the same apply is unknown until then.
variable "enable_https" {
  type    = bool
  default = false
}

# ACM certificate ARN for the :443 (web) and :8443 (sandbox) listeners when
# enable_https. Used only as a listener attribute, never in count, so it may be
# an apply-time value. Must be issued in the ALB's region.
variable "certificate_arn" {
  type    = string
  default = ""
}

# ALB HTTP/2. Some corporate SSL-inspection proxies break h2 framing, failing
# static JS chunks with ERR_HTTP2_PROTOCOL_ERROR while the box itself is fine;
# false pins the ALB to HTTP/1.1 for those networks.
variable "enable_http2" {
  type    = bool
  default = true
}
