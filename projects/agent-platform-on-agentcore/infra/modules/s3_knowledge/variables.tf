variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "create_source_bucket" {
  description = <<-EOT
    Create a bucket for documents a knowledge base reads rather than receives.

    On by default so the stack owns one usable source out of the box. Turn it off if
    every source bucket is managed elsewhere; `knowledge_source_buckets` can name
    those regardless of whether this one exists.
  EOT
  type        = bool
  default     = true
}

variable "bucket_suffix" {
  description = "Optional suffix appended to bucket names for global uniqueness (e.g. -<account_id>). Empty keeps the bare <project>-<purpose>-<region> name."
  type        = string
  default     = ""
}
