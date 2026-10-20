#!/usr/bin/env python3
"""
Put the guardrail's content filters on a safeguard tier, in place.

The `aws` provider this stack pins (5.x) has no tier or cross-Region setting on
`aws_bedrock_guardrail`, and moving the guardrail to `awscc` would replace it:
a new id, and every deployed runtime still pointing at the old one. So the
resource keeps owning the guardrail and this script adds the tier afterwards,
through UpdateGuardrail with everything else carried over unchanged.

Why the tier matters (bst load test, 2026-10-18): the Classic tier evaluates
English, French and Spanish only. On this Korean-language platform it let all
three Korean prompt attacks of a ten-attack sample through, and at HIGH strength
it blocked 8 of 12 ordinary English instructions ("Respond only with JSON",
"Reply in the chat (no artifact)…") as prompt attacks. The Standard tier caught
10 of 10 attacks at HIGH confidence. The strengths it runs at are calibrated in
../variables.tf.

Usage:
  guardrail_tier.py apply --guardrail-id ID --region R --tier STANDARD --profile us.guardrail.v1:0
  guardrail_tier.py apply --guardrail-id ID --region R --tier CLASSIC
"""
import argparse
import sys
import time

import boto3

# Fields of a filter that GetGuardrail returns and UpdateGuardrail accepts as is.
_FILTER_FIELDS = (
    "type", "inputStrength", "outputStrength", "inputModalities", "outputModalities",
    "inputAction", "outputAction", "inputEnabled", "outputEnabled",
)


def _content_policy(current: dict, tier: str) -> dict:
    filters = [
        {k: f[k] for k in _FILTER_FIELDS if k in f}
        for f in (current.get("contentPolicy") or {}).get("filters") or []
    ]
    return {"filtersConfig": filters, "tierConfig": {"tierName": tier}}


def apply(guardrail_id: str, region: str, tier: str, profile: str) -> None:
    bedrock = boto3.client("bedrock", region_name=region)
    current = bedrock.get_guardrail(guardrailIdentifier=guardrail_id, guardrailVersion="DRAFT")
    have_tier = ((current.get("contentPolicy") or {}).get("tier") or {}).get("tierName", "CLASSIC")
    have_profile = (current.get("crossRegionDetails") or {}).get("guardrailProfileId", "")
    if have_tier == tier and (tier == "CLASSIC" or have_profile == profile):
        print(f"guardrail {guardrail_id}: already {tier} {have_profile}".rstrip())
        return

    update = {
        "guardrailIdentifier": guardrail_id,
        "name": current["name"],
        "blockedInputMessaging": current["blockedInputMessaging"],
        "blockedOutputsMessaging": current["blockedOutputsMessaging"],
        "contentPolicyConfig": _content_policy(current, tier),
    }
    if current.get("description"):
        update["description"] = current["description"]
    if current.get("kmsKeyArn"):
        update["kmsKeyId"] = current["kmsKeyArn"]
    if tier != "CLASSIC":
        # The Standard tier runs only through a guardrail profile.
        update["crossRegionConfig"] = {"guardrailProfileIdentifier": profile}
    # This module configures content filters only. A topic, word, PII or
    # grounding policy would be dropped by the update; refuse rather than lose it.
    for key in ("topicPolicy", "wordPolicy", "sensitiveInformationPolicy", "contextualGroundingPolicy", "automatedReasoningPolicy"):
        if current.get(key):
            sys.exit(f"guardrail {guardrail_id} has a {key}; this script would drop it")

    bedrock.update_guardrail(**update)
    for _ in range(60):
        status = bedrock.get_guardrail(guardrailIdentifier=guardrail_id, guardrailVersion="DRAFT")["status"]
        if status == "READY":
            print(f"guardrail {guardrail_id}: {have_tier} -> {tier} {profile if tier != 'CLASSIC' else ''}".rstrip())
            return
        if status == "FAILED":
            sys.exit(f"guardrail {guardrail_id}: update FAILED")
        time.sleep(2)
    sys.exit(f"guardrail {guardrail_id}: still {status} after 120s")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--guardrail-id", required=True)
    a.add_argument("--region", required=True)
    a.add_argument("--tier", choices=("STANDARD", "CLASSIC"), required=True)
    a.add_argument("--profile", default="")
    args = parser.parse_args()
    if args.tier != "CLASSIC" and not args.profile:
        parser.error("--profile is required for the STANDARD tier")
    apply(args.guardrail_id, args.region, args.tier, args.profile)


if __name__ == "__main__":
    main()
