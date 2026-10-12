"""One-off backfill of the usage rollup from stored threads.

Recovers turns, tool calls, distinct users and thread starts. **Not tokens** —
usage was never written into the transcript, so there is nothing to recover, and
the token attributes are left absent rather than zero so the dashboard can say
"unknown" instead of "free".

Idempotency: this is `ADD`-based, so running it twice doubles every counter. Run
it once, against a table that has no live traffic yet.
"""
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def backfill_thread(
    thread: Any, usage_service: Any, before: Optional[str] = None
) -> int:
    """Record one turn per stored assistant message. Returns how many.

    `before` is an exclusive `YYYY-MM-DD` cutoff. It exists because this script
    is `ADD`-based and therefore not idempotent: any day the live stream already
    counted must be excluded, or that day doubles with no way to undo it.
    """
    day = (thread.created_at or "")[:10]
    if before and day >= before:
        return 0
    messages = (thread.values or {}).get("messages") or []
    recorded = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("type") != "ai":
            continue
        tool_calls: dict = {}
        for call in message.get("tool_calls") or []:
            name = call.get("name")
            if name:
                tool_calls[name] = tool_calls.get(name, 0) + 1
        usage_service.record_turn(
            agent_record_id=thread.agent_record_id,
            owner_sub=thread.owner_sub,
            input_tokens=0,
            output_tokens=0,
            tool_calls=tool_calls,
            thread_started=recorded == 0,
            date=day or None,
        )
        recorded += 1
    return recorded


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--before",
        help="Only backfill threads created strictly before this YYYY-MM-DD. "
        "Set it to the date live collection started; without it, days the "
        "stream already counted are doubled and there is no undo.",
    )
    args = parser.parse_args()

    from core.config import AWS_REGION, DYNAMODB_THREADS_TABLE, USAGE_TABLE
    from repositories.thread_repository import ThreadRepository
    from repositories.usage_repository import UsageRepository
    from services.usage_service import UsageService

    if not USAGE_TABLE:
        raise SystemExit("USAGE_TABLE is not set")

    threads = ThreadRepository(
        table_name=DYNAMODB_THREADS_TABLE, region_name=AWS_REGION
    )
    usage = UsageService(
        repository=UsageRepository(table_name=USAGE_TABLE, region_name=AWS_REGION)
    )

    total_threads = 0
    total_turns = 0
    # A full pass over history is the one place a Scan is correct: it is a one-off
    # migration over every thread, not a query path.
    response = threads.table.scan()
    while True:
        for item in response.get("Items", []):
            thread = threads._to_thread(item)
            total_turns += backfill_thread(thread, usage, before=args.before)
            total_threads += 1
        key = response.get("LastEvaluatedKey")
        if not key:
            break
        response = threads.table.scan(ExclusiveStartKey=key)

    print(f"backfilled {total_turns} turns from {total_threads} threads")


if __name__ == "__main__":
    main()
