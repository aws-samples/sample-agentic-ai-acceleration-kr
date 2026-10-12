variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "bucket_suffix" {
  description = "Optional suffix appended to bucket names for global uniqueness (e.g. -<account_id>). Empty keeps the bare <project>-<purpose>-<region> name."
  type        = string
  default     = ""
}
