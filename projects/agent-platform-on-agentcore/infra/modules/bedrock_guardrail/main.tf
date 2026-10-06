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
      input_strength  = var.content_filter_strength
      output_strength = "NONE"
    }

    dynamic "filters_config" {
      for_each = ["HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"]
      content {
        type            = filters_config.value
        input_strength  = var.content_filter_strength
        output_strength = var.content_filter_strength
      }
    }
  }

  # No sensitive-information (PII) policy on purpose. ANONYMIZE masks only the
  # model's output, never the input (verified: an input with an email returns
  # action=NONE), so it cannot protect the prompt — and on the output it rewrites
  # every person name it sees, including the assistant's own ("저는 {NAME}이며…"),
  # which is noise, not safety. Content filters are the safety layer here.
}
