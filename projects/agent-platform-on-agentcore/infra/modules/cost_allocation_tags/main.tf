# Cost allocation tag activation, so per-agent billed cost has data.
#
# `Platform` and `AgentName` are put on every harness at creation
# (`server/services/harness_service.py`) and propagate to the companion Runtime
# and Memory. Tagging a resource is not enough on its own: until the key is
# **Active** in Billing, Cost Explorer refuses to group by it and the per-agent
# card on /insights reads `집계 중`.
#
# Three properties of this API shape the module:
#
# 1. **Account-global.** The status is not regional and not per-stack. Two
#    Terraform states in one account must not both declare these keys or they
#    will fight over one resource — which is why the envs gate the module behind
#    a variable instead of always including it.
# 2. **The key must be registered first.** Billing lists a tag key only after
#    observing it on *billed usage*, which lags up to 24 hours. Measured
#    2026-08-16: minutes after a probe harness put the tags on seven live
#    resources, and the resource tagging API listed them,
#    `UpdateCostAllocationTagsStatus` still answered `ValidationException:
#    Failed to update Cost Allocation Tag: Tag keys not found:
#    Platform,AgentName`. On a fresh account this module therefore fails until a
#    harness has been created and used — leave it off for the first apply.
# 3. **Activation is not retroactive.** Cost data accrues from activation
#    forward, so the sooner it succeeds the sooner the per-agent card has
#    anything in it. Destroying this resource sets the key back to Inactive; it
#    does not remove the data already attributed.
resource "aws_ce_cost_allocation_tag" "this" {
  for_each = toset(var.tag_keys)

  tag_key = each.value
  status  = "Active"
}
