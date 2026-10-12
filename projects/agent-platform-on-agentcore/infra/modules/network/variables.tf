variable "project" {
  type = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "azs" {
  description = "Two availability zones"
  type        = list(string)
}

variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.0.0.0/24", "10.0.1.0/24"]
}

variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.0.10.0/24", "10.0.11.0/24"]
}

variable "alb_ingress_cidrs" {
  description = "CIDRs allowed inbound to the ALB (80/8081, plus 443/8443 when enable_https). Default open; set to office IPs or an external HTTPS proxy to lock down."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "enable_https" {
  description = "Open ALB ingress on 443 (web) and 8443 (sandbox). Set true when an ACM certificate is attached; the ecs module adds the matching HTTPS listeners."
  type        = bool
  default     = false
}

variable "hibernate" {
  description = "Delete NAT gateway and EIP for cost savings, removing private-subnet egress. Tasks must be scaled to 0 (desired_count=0) or they lose egress to ECR, Bedrock, and external services."
  type        = bool
  default     = false
}
