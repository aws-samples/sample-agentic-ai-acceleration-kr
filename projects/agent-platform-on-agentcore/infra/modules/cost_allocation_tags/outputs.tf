output "active_tag_keys" {
  description = "Tag keys this module holds Active in Billing."
  value       = sort([for tag in aws_ce_cost_allocation_tag.this : tag.tag_key])
}
