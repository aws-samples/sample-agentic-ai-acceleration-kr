"""Give threads written before the list indexes existed what those indexes need.

The thread listing reads three secondary indexes (infra/modules/dynamodb):
by owner, by agent, and `scope` for the admin's every-thread view. A thread
written by an older server has no `scope` (so the admin list cannot see it) and
no `summary` (so its row falls back to reading the whole transcript), and may
hold `owner_sub = ""` / `agent_record_id = ""` — empty strings, which DynamoDB
refuses as an index key, so such an item could not even be updated until they go.

Per thread this sets `scope` and `summary` and removes those empty attributes,
in one conditional update: only if the item still exists and its `updated_at`
is the one scanned, so a turn saved meanwhile (which writes all of this itself)
is never overwritten with a stale summary. Rerunning is safe; threads already
carrying both fields are skipped.

Usage:
    python scripts/backfill_thread_index.py --dry-run
    python scripts/backfill_thread_index.py [--segments 8]
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

from repositories.thread_repository import SCOPE_ALL, summary_of  # noqa: E402


def needs_backfill(item) -> bool:
    return (
        "scope" not in item
        or "summary" not in item
        or item.get("owner_sub") == ""
        or item.get("agent_record_id") == ""
    )


def backfill_one(table, item) -> str:
    sets = {"#scope": ("scope", SCOPE_ALL),
            "#summary": ("summary", json.dumps(summary_of(json.loads(item.get("values") or "{}")),
                                               ensure_ascii=False))}
    removes = [name for name in ("owner_sub", "agent_record_id") if item.get(name) == ""]
    expression = "SET " + ", ".join(f"{alias} = :{value[0]}" for alias, value in sets.items())
    if removes:
        expression += " REMOVE " + ", ".join(removes)
    try:
        table.update_item(
            Key={"thread_id": item["thread_id"]},
            UpdateExpression=expression,
            ConditionExpression="attribute_exists(thread_id) AND updated_at = :seen",
            ExpressionAttributeNames={alias: value[0] for alias, value in sets.items()},
            ExpressionAttributeValues={
                **{f":{value[0]}": value[1] for value in sets.values()},
                ":seen": item["updated_at"],
            },
        )
        return "updated"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "changed"  # written by a live server since the scan: already current
        raise


def scan_segment(table, segment, total, dry_run):
    counts = {"seen": 0, "updated": 0, "changed": 0, "current": 0}
    kwargs = {"Segment": segment, "TotalSegments": total}
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            counts["seen"] += 1
            if not needs_backfill(item):
                counts["current"] += 1
            elif dry_run:
                counts["updated"] += 1
            else:
                counts[backfill_one(table, item)] += 1
        if "LastEvaluatedKey" not in page:
            return counts
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", default=os.getenv("DYNAMODB_THREADS_TABLE"))
    parser.add_argument("--region", default=os.getenv("AWS_REGION"))
    parser.add_argument("--segments", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.table:
        raise SystemExit("--table (or DYNAMODB_THREADS_TABLE) is required")
    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    with ThreadPoolExecutor(args.segments) as pool:
        results = list(pool.map(lambda s: scan_segment(table, s, args.segments, args.dry_run),
                                range(args.segments)))
    total = {k: sum(r[k] for r in results) for k in results[0]}
    verb = "would update" if args.dry_run else "updated"
    print(f"{total['seen']} threads: {verb} {total['updated']}, already current {total['current']}, "
          f"changed since scan {total['changed']}")


if __name__ == "__main__":
    main()
