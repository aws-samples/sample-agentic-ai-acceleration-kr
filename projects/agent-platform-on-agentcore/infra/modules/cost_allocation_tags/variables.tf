variable "tag_keys" {
  type        = list(string)
  description = <<-EOT
    Tag keys to activate for cost allocation. The defaults are the two the
    platform puts on every harness: `Platform` groups the whole stack and
    `AgentName` is what splits Cost Explorer per agent.
  EOT
  default     = ["Platform", "AgentName"]
}
