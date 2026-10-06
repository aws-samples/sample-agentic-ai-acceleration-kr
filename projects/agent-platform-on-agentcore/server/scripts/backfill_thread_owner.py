"""Assign an owner to threads created before ownership was enforced.

Threads written before `owner_sub` existed carry no owner, and the enforcement
in ThreadService.require_owned fails closed — so until they are backfilled they
are readable by nobody. This gives them one.

Only items with no `owner_sub` are touched, via a conditional write, so a rerun
cannot take a thread away from its real owner. Threads whose owner is already
set are left alone even if --owner-sub differs.

Usage:
    python scripts/backfill_thread_owner.py --owner-sub <cognito-sub> --dry-run
    python scripts/backfill_thread_owner.py --owner-sub <cognito-sub>

Find the sub for an email with:
    aws cognito-idp list-users --user-pool-id <pool> \
        --filter 'email="admin@example.com"' \
        --query 'Users[].Attributes[?Name==`sub`].Value' --output text
"""
import argparse
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")


def unowned_threads(table):
    """Every thread with no owner, following pagination.

    A scan, not a query: the table has no index on an attribute that is by
    definition absent.
    """
    items = []
    kwargs = {
        "FilterExpression": "attribute_not_exists(owner_sub) OR owner_sub = :empty",
        "ExpressionAttributeValues": {":empty": ""},
        "ProjectionExpression": "thread_id, created_at",
    }
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def claim(table, thread_id, owner_sub):
    """Set owner_sub only if it is still absent or empty.

    The condition is what makes a rerun safe: between the scan above and this
    write, a live server may have created or claimed the thread.
    """
    try:
        table.update_item(
            Key={"thread_id": thread_id},
            UpdateExpression="SET owner_sub = :owner",
            ConditionExpression=(
                "attribute_not_exists(owner_sub) OR owner_sub = :empty"
            ),
            ExpressionAttributeValues={":owner": owner_sub, ":empty": ""},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner-sub",
        required=True,
        help="Cognito sub to assign. Not a username: usernames are reassignable.",
    )
    parser.add_argument(
        "--table",
        default=os.getenv("DYNAMODB_THREADS_TABLE", "langgraph-threads"),
    )
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-northeast-1"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # A username here would silently own every legacy thread under a string that
    # no token will ever present, leaving them unreachable in a way that looks
    # like a successful migration.
    if "@" in args.owner_sub:
        parser.error(
            "--owner-sub looks like an email. Pass the Cognito sub (a UUID); "
            "see this script's docstring for the lookup command."
        )

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    pending = unowned_threads(table)

    print(f"Table {args.table} ({args.region}): {len(pending)} thread(s) with no owner")
    for item in sorted(pending, key=lambda i: i.get("created_at", "")):
        print(f"  {item['thread_id']}  created {item.get('created_at', '?')}")

    if not pending:
        return 0
    if args.dry_run:
        print(f"\n--dry-run: would assign owner_sub={args.owner_sub} to the above.")
        return 0

    claimed = sum(claim(table, item["thread_id"], args.owner_sub) for item in pending)
    skipped = len(pending) - claimed
    print(f"\nAssigned owner_sub={args.owner_sub} to {claimed} thread(s).")
    if skipped:
        print(f"{skipped} already had an owner by the time of the write; left alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
