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

variable "custom_metadata_schema" {
  type        = map(string)
  description = <<-EOT
    Custom metadata schema: record type ("DEFAULT", "MCP", "AGENT", "SKILL",
    "CUSTOM", "GATEWAY") => JSON Schema (draft-07, flat object; fields are
    text, enum, url or boolean). Records carry typed values the UI collects and
    search filters on (customMetadata.<field>). AWS only ever lets a schema
    grow — a saved field cannot be removed or retyped — so the default keeps
    every field optional, and the sync merges it over the live schema so a field
    dropped here (docs_url, 2026-10-10) stays on registries that already have
    it. `owner` is filled by the server with the registering user, not by a
    form. Empty map = leave the registry without a schema. A null from the
    caller means "use this default" (nullable = false).
  EOT
  nullable    = false
  default = {
    DEFAULT = "{\"type\":\"object\",\"properties\":{\"owner\":{\"type\":\"string\"},\"team\":{\"type\":\"string\"},\"tier\":{\"type\":\"string\",\"enum\":[\"internal\",\"partner\",\"public\"]}}}"
  }
}

variable "kms_key_arn" {
  type        = string
  description = "Customer managed KMS key encrypting registry data. Creation-time only in AWS: set it before the first apply, or the registry keeps the AWS owned key. Not a replacement trigger on purpose — replacing this module's resource deletes the live registry and its records."
  default     = ""
}

variable "approval_email" {
  type        = string
  description = "Address subscribed to the record-approval events topic (Pending Approval / Rejected / Deprecated, via EventBridge). Empty = topic without a subscription."
  default     = ""
}

variable "enable_approval_events" {
  type        = bool
  description = "Create the EventBridge rule and SNS topic that announce record approval-workflow transitions (source aws.agent-registry)."
  default     = true
}

variable "create_sync_role" {
  type        = bool
  description = "Create the IAM role AWS Agent Registry assumes to SigV4-sign synchronisation fetches against servers on AgentCore Runtime/Gateway. The server offers it as the 'IAM' credential in the Register dialog."
  default     = true
}
