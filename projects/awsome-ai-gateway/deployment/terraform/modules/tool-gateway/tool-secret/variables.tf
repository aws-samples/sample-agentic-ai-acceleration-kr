variable "name" {
  type        = string
  description = "Full secret name, e.g. websearch-gw/dev/tool/serper"
}

variable "description" {
  type        = string
  description = "Human-readable description of the secret."
  default     = ""
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to the secret."
  default     = {}
}
