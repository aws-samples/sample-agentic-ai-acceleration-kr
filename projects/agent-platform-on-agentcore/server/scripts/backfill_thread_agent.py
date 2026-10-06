"""Pin threads created before an agent was recorded on them.

A thread is bound to one registry agent (`Thread.agent_record_id`), because
AgentCore Memory scopes events by a session id derived from the thread id alone —
two agents in one thread either share an event stream or lose the history to each
other's memory. Threads written before that field existed carry no agent, and
`ThreadService._pin_agent` refuses to continue the ones that already have turns:
which agent wrote that history is not recorded, and guessing would assert a fact
we do not have.

This assigns one. It does not make the guess correct — it declares it. Use it
when you know a deployment only ever had one chattable agent, which is the case
it exists for. If several agents were in use, prefer leaving the threads
unpinned: they still render from DynamoDB and can be read, just not continued.

Only items with no `agent_record_id` are touched, via a conditional write, so a
rerun cannot repoint a thread that has since been pinned for real.

Usage:
    python scripts/backfill_thread_agent.py --record-id <id> --dry-run
    python scripts/backfill_thread_agent.py --record-id <id> --agent-name "My Agent"

List the chattable records to pick from with:
    aws dynamodb scan --table-name <registry-table> \
        --query 'Items[].{id:record_id.S,name:name.S}' --output table
"""
import argparse
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")


def unpinned_threads(table):
    """Every thread with no agent, following pagination.

    A scan, not a query: the table has no index on an attribute that is by
    definition absent.
    """
    items = []
    kwargs = {
        "FilterExpression": (
            "attribute_not_exists(agent_record_id) OR agent_record_id = :empty"
        ),
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


def pin(table, thread_id, record_id, agent_name):
    """Set the agent only if it is still absent or empty.

    The condition is what makes a rerun safe: between the scan above and this
    write, a live server may have pinned the thread on its first turn.
    """
    try:
        table.update_item(
            Key={"thread_id": thread_id},
            UpdateExpression=(
                "SET agent_record_id = :record, agent_name = :name"
            ),
            ConditionExpression=(
                "attribute_not_exists(agent_record_id) OR agent_record_id = :empty"
            ),
            ExpressionAttributeValues={
                ":record": record_id,
                ":name": agent_name,
                ":empty": "",
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--record-id",
        required=True,
        help="Registry record id of the agent to pin these threads to.",
    )
    parser.add_argument(
        "--agent-name",
        default="",
        help=(
            "Display name for the sidebar. Only a label: the record id is what "
            "decides which agent may answer."
        ),
    )
    parser.add_argument(
        "--table",
        default=os.getenv("DYNAMODB_THREADS_TABLE", "langgraph-threads"),
    )
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-northeast-1"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    pending = unpinned_threads(table)

    print(
        f"Table {args.table} ({args.region}): "
        f"{len(pending)} thread(s) with no agent"
    )
    for item in sorted(pending, key=lambda i: i.get("created_at", "")):
        print(f"  {item['thread_id']}  created {item.get('created_at', '?')}")

    if not pending:
        return 0
    if args.dry_run:
        print(
            f"\n--dry-run: would pin the above to agent_record_id={args.record_id}"
            f" (name={args.agent_name or '<unset>'})."
        )
        return 0

    pinned = sum(
        pin(table, item["thread_id"], args.record_id, args.agent_name)
        for item in pending
    )
    skipped = len(pending) - pinned
    print(f"\nPinned {pinned} thread(s) to agent_record_id={args.record_id}.")
    if skipped:
        print(f"{skipped} had been pinned by the time of the write; left alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
