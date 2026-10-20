terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    null = {
      source = "hashicorp/null"
    }
  }
}

# A Bedrock Guardrail applied to every model call the agent runtime makes, so
# input and output are filtered before and after the model runs. The runtime
# attaches it by id + version through the GUARDRAIL_ID / GUARDRAIL_VERSION env
# vars (see agent-runtime/config.py and agents/base.py); this resource only owns
# the guardrail itself. Created in the provider's region (var.region), the same
# region the runtime calls bedrock-runtime in — a guardrail is region-scoped.
resource "aws_bedrock_guardrail" "this" {
  name        = "${var.project}-guardrail"
  description = "Input/output safety for the ${var.project} agent runtime."

  # Shown to the user in place of the model turn when a filter blocks it. The
  # runtime streams these through unchanged, so they must read as a normal reply.
  blocked_input_messaging   = "요청을 처리할 수 없습니다. 안전 정책에 위배되는 내용이 감지되었습니다."
  blocked_outputs_messaging = "응답을 표시할 수 없습니다. 안전 정책에 위배되는 내용이 감지되었습니다."

  content_policy_config {
    # PROMPT_ATTACK is an input-only category; Bedrock rejects any output
    # strength other than NONE for it, so it is declared apart from the rest.
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = var.prompt_attack_strength
      output_strength = "NONE"
    }

    dynamic "filters_config" {
      for_each = ["HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"]
      content {
        type            = filters_config.value
        input_strength  = var.content_input_strength
        output_strength = var.content_output_strength
      }
    }
  }

  # No sensitive-information (PII) policy on purpose. ANONYMIZE masks only the
  # model's output, never the input (verified: an input with an email returns
  # action=NONE), so it cannot protect the prompt — and on the output it rewrites
  # every person name it sees, including the assistant's own ("저는 {NAME}이며…"),
  # which is noise, not safety. Content filters are the safety layer here.
}

# The safeguard tier and its cross-Region profile, set in place by a script
# because the pinned aws provider cannot (see scripts/guardrail_tier.py). Every
# input of the guardrail above is a trigger: Terraform's own update of the
# guardrail sends no tier and would put it back on Classic, so the script runs
# again after it.
locals {
  tier_script = "${path.module}/scripts/guardrail_tier.py"
  # The guardrail profile of the region's geography.
  guardrail_profile = (
    startswith(var.region, "us-gov-") ? "us-gov.guardrail.v1:0" :
    startswith(var.region, "us-") ? "us.guardrail.v1:0" :
    var.region == "eu-west-2" ? "uk.guardrail.v1:0" :
    startswith(var.region, "eu-") || var.region == "il-central-1" ? "eu.guardrail.v1:0" :
    startswith(var.region, "ca-") ? "ca.guardrail.v1:0" :
    "apac.guardrail.v1:0"
  )
}

resource "null_resource" "tier" {
  triggers = {
    guardrail_id    = aws_bedrock_guardrail.this.guardrail_id
    tier            = var.tier
    profile         = local.guardrail_profile
    script_sha      = filesha256(local.tier_script)
    content_input   = var.content_input_strength
    content_output  = var.content_output_strength
    prompt_strength = var.prompt_attack_strength
    blocked_input   = aws_bedrock_guardrail.this.blocked_input_messaging
    blocked_output  = aws_bedrock_guardrail.this.blocked_outputs_messaging
  }

  provisioner "local-exec" {
    command = join(" ", [
      "python3", local.tier_script, "apply",
      "--guardrail-id", aws_bedrock_guardrail.this.guardrail_id,
      "--region", var.region,
      "--tier", var.tier,
      "--profile", local.guardrail_profile,
    ])
  }
}
