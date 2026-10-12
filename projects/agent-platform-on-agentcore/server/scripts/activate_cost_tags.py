"""Activate `Platform`/`AgentName` as cost allocation tags.

Per-agent billed cost needs these two keys Active in Billing. Terraform owns
that status now — `infra/modules/cost_allocation_tags`, wired into
`envs/standalone` — so this script is the fallback for the ordering problem
Terraform cannot solve on its own:

1. The billing tag registry only lists a key after it has been observed on
   *billed usage*, which lags up to 24 hours. Measured 2026-08-16: minutes after
   a probe harness put the tags on seven live resources, and the resource tagging
   API listed them, `UpdateCostAllocationTagsStatus` still answered
   `ValidationException: Failed to update Cost Allocation Tag: Tag keys not
   found: Platform,AgentName`. A first apply into a fresh account therefore
   fails, which is why DEPLOYMENT.md has a fresh account apply with
   `-var activate_cost_allocation_tags=false` first.
2. Activation is not retroactive. Cost data accrues from activation forward, so
   the sooner it succeeds the sooner the per-agent card has anything in it —
   waiting for the next full apply costs attribution.

So: use it to get the keys Active as soon as a harness has been created and used,
re-running the next day if it reports `not_registered`, then let the Terraform
resource hold them there. Both paths converge on the same account-global status,
and running this after Terraform already activated them is a no-op
(`already_active`).

    python -m scripts.activate_cost_tags
"""
import argparse
import json
import sys
from typing import Any, Dict, List, Sequence

import boto3

DEFAULT_KEYS = ("Platform", "AgentName")


def activate(client, keys: Sequence[str]) -> Dict[str, Any]:
    """Bring `keys` to Active, reporting which of three states we are in."""
    listed = {
        tag["TagKey"]: tag.get("Status")
        for tag in client.list_cost_allocation_tags(
            TagKeys=list(keys)
        ).get("CostAllocationTags", [])
    }

    missing = [key for key in keys if key not in listed]
    inactive = [key for key in keys if listed.get(key) == "Inactive"]
    active = [key for key in keys if listed.get(key) == "Active"]

    if not listed:
        return {
            "status": "not_registered",
            "keys": list(keys),
            "detail": (
                "The billing tag registry has never seen these keys. A tagged "
                "resource must accrue billed usage first, and registration lags "
                "up to 24 hours. Create or invoke one agent, then re-run "
                "tomorrow. Cost data is not retroactive, so this is worth "
                "retrying until it succeeds."
            ),
        }

    if not inactive:
        return {
            "status": "already_active",
            "keys": active,
            "detail": "Nothing to do.",
        }

    response = client.update_cost_allocation_tags_status(
        CostAllocationTagsStatus=[
            {"TagKey": key, "Status": "Active"} for key in inactive
        ]
    )
    # `Errors` is the response's *only* member, a list of {TagKey, Code, Message}:
    # this API reports per-key failure inside a 200. Ignoring it would print
    # "activated" for a key that was refused, which is the one thing this script
    # exists not to do.
    errors = response.get("Errors") or []
    failed = {entry.get("TagKey"): entry for entry in errors if entry.get("TagKey")}
    activated = [key for key in inactive if key not in failed]

    detail = "Cost data accrues from now on; it is not backfilled."
    if missing:
        detail += (
            f" Not yet registered in Billing, so left alone: {', '.join(missing)}."
        )
    if failed:
        refused = ", ".join(
            f"{key} ({entry.get('Code')}: {entry.get('Message')})"
            for key, entry in sorted(failed.items())
        )
        detail += f" Refused by Billing: {refused}."

    if not activated:
        return {"status": "failed", "keys": [], "detail": detail}
    return {
        "status": "partial" if failed else "activated",
        "keys": activated,
        "detail": detail,
    }


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key",
        action="append",
        dest="keys",
        help="Tag key to activate; repeatable. Defaults to Platform and AgentName.",
    )
    args = parser.parse_args(argv)
    client = boto3.client("ce", region_name="us-east-1")
    result = activate(client, args.keys or list(DEFAULT_KEYS))
    print(json.dumps(result, indent=2))
    # Non-zero for anything that needs a human to come back: nothing registered
    # yet, or Billing refused every key. `partial` exits 0 with the refusal named
    # in `detail`, because something did land and re-running is safe either way.
    return 1 if result["status"] in ("not_registered", "failed") else 0


if __name__ == "__main__":
    sys.exit(main())
