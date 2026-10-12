"""Enable managed memory on harnesses created before it was the default.

Harnesses built earlier carry `memory: {"disabled": {}}`. Their clients now send
only the newest message, so without memory those agents lose all context.
UpdateHarness accepts a memory configuration, so recreation is not required.

Turns from before the migration are not in Memory and cannot be recovered — they
still render from DynamoDB, but recall starts from the migration forward.

Usage:
    python scripts/migrate_harness_memory.py --dry-run
    python scripts/migrate_harness_memory.py
"""
import argparse
import os
import sys

import boto3

# Must match services/harness_service.py's create_harness. Expiry is pinned
# because the AWS default is 30 days, which would silently expire conversations.
# SUMMARIZATION only (session-scoped); SEMANTIC is left off because its namespace
# is actor-scoped and leaks facts across a user's chats — see harness_service.py.
MEMORY = {
    "managedMemoryConfiguration": {
        "strategies": ["SUMMARIZATION"],
        "eventExpiryDuration": 365,
    }
}


def list_harnesses(control):
    """Every harness, following pagination."""
    harnesses = []
    token = None
    while True:
        params = {"maxResults": 100}
        if token:
            params["nextToken"] = token
        resp = control.list_harnesses(**params)
        harnesses.extend(resp.get("harnesses", []))
        token = resp.get("nextToken")
        if not token:
            return harnesses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "ap-northeast-1"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    control = boto3.client("bedrock-agentcore-control", region_name=args.region)

    migrated = skipped = failed = 0
    for summary in list_harnesses(control):
        harness_id = summary.get("harnessId")
        name = summary.get("harnessName", harness_id)

        # ListHarnesses omits the configuration, so each one has to be read.
        try:
            detail = control.get_harness(harnessId=harness_id).get("harness", {})
        except Exception as exc:
            print(f"FAILED {name}: could not read: {exc}", file=sys.stderr)
            failed += 1
            continue

        status = detail.get("status")
        if status not in ("READY", "UPDATE_FAILED"):
            # CREATING/DELETING cannot take an update; a failed create has no
            # working harness to migrate.
            print(f"skip {name}: status {status}")
            skipped += 1
            continue

        if "disabled" not in (detail.get("memory") or {}):
            print(f"skip {name}: memory already configured")
            skipped += 1
            continue

        if args.dry_run:
            print(f"would migrate {name} ({harness_id})")
            migrated += 1
            continue

        try:
            control.update_harness(
                harnessId=harness_id, memory={"optionalValue": MEMORY}
            )
            print(f"migrated {name}")
            migrated += 1
        except Exception as exc:
            print(f"FAILED {name}: {exc}", file=sys.stderr)
            failed += 1

    verb = "would migrate" if args.dry_run else "migrated"
    print(f"\n{migrated} {verb}, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
