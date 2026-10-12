variable "project" {
  description = "Name prefix for the guardrail, matching the rest of the stack."
  type        = string
}

variable "content_filter_strength" {
  description = "Strength applied to every content filter, input and output (NONE/LOW/MEDIUM/HIGH)."
  type        = string
  default     = "HIGH"
}
