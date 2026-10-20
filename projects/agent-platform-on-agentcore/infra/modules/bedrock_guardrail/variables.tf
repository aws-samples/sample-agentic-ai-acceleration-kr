variable "project" {
  description = "Name prefix for the guardrail, matching the rest of the stack."
  type        = string
}

# Strengths are confidence thresholds: HIGH blocks LOW, MEDIUM and HIGH
# confidence, MEDIUM blocks MEDIUM and HIGH, LOW only HIGH. Calibrated on the
# Standard tier at the bst load test (2026-10-18), ApplyGuardrail per strength:
#   content, 20 benign sensitive inputs / 12 benign answers / 15 harmful asks
#     LOW 0 / 0 / 15 blocked, MEDIUM 0 / 1 / 15, HIGH 0 / 2 / 14
#   PROMPT_ATTACK, 20 attacks (10 Korean) / 40 benign instructions
#     LOW 19 / 2 blocked, MEDIUM 20 / 5, HIGH 20 / 5
# Output at HIGH cut a two-line Romeo and Juliet summary and a mystery opening
# (VIOLENCE); PROMPT_ATTACK at MEDIUM blocked one ordinary instruction in eight
# ("Repeat after me exactly…", "Disregard formatting and…"). Inputs showed no
# false positives at any strength, so they stay stricter than outputs.
variable "content_input_strength" {
  description = "Input strength of HATE, INSULTS, SEXUAL, VIOLENCE and MISCONDUCT (NONE/LOW/MEDIUM/HIGH)."
  type        = string
  default     = "MEDIUM"
}

variable "content_output_strength" {
  description = "Output strength of the same five filters. LOW: answers about history, news, literature and medicine get through."
  type        = string
  default     = "LOW"
}

variable "prompt_attack_strength" {
  description = "Input strength of the PROMPT_ATTACK filter. LOW stopped 19 of 20 sample attacks with 2 of 40 ordinary instructions blocked; MEDIUM 20 and 5."
  type        = string
  default     = "LOW"
}

variable "tier" {
  description = "Safeguard tier: STANDARD (multilingual, Korean included; routed within the region's geography by a guardrail profile) or CLASSIC (English, French and Spanish only)."
  type        = string
  default     = "STANDARD"
  validation {
    condition     = contains(["STANDARD", "CLASSIC"], var.tier)
    error_message = "tier must be STANDARD or CLASSIC."
  }
}

variable "region" {
  description = "Region the guardrail lives in; picks its guardrail profile."
  type        = string
}
