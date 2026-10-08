variable "project" {
  type        = string
  description = "Project name prefix; the registry is named \"<project>-registry\"."
}

variable "region" {
  type        = string
  description = "AWS region hosting the registry."
}

variable "description" {
  type        = string
  description = "Registry description."
  default     = "Agent platform registry - agents, MCP tools and skills catalog"
}

variable "auto_approval" {
  type        = bool
  description = "When true, new records are approved on creation (no manual approval step)."
  default     = true
}

variable "python_bin" {
  type        = string
  description = "Python interpreter used to run registry.py. Needs a boto3 new enough to know the agent-registry-control service (botocore >= 1.43). Point at a venv/uv python where the host's system python3 is too old."
  default     = "python3"
}
