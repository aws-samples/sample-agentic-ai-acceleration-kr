variable "repositories" {
  description = "ECR repository names"
  type        = list(string)
  default     = ["bap/server", "bap/web", "bap/agent-runtime"]
}
