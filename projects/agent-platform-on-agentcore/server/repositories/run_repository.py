"""Run leases: which server task holds the chat turn in flight on a thread.

One row per thread with a run, keyed by `thread_id`:

    run_id        this run (a reservation's uuid); every write after the claim
                  is conditional on it, so a run never touches a successor's row
    holder        "<ip>:<port>" of the task running it
    owner_sub     who started it (a Stop must come from them)
    lease_until   epoch milliseconds; past it the row is free to claim
    expires_at    epoch seconds, the table's TTL, an hour past the lease, to
                  sweep rows a killed task never deleted
    cancel_requested  set by a task that could not reach the holder; the
                  holder reads it on its next renewal

Times are integers so the conditions compare numbers, not ISO strings.
"""
from typing import Any, Dict, Optional

from botocore.exceptions import ClientError

from repositories.base import DynamoDBRepository

# How long past the lease the TTL sweep may take the row. DynamoDB deletes
# expired items within days, not seconds, so readers never trust the TTL; the
# lease decides.
_TTL_GRACE_SECONDS = 3600


def _conditional_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class RunRepository(DynamoDBRepository):
    def acquire(
        self,
        thread_id: str,
        run_id: str,
        holder: str,
        owner_sub: Optional[str],
        now_ms: int,
        lease_ms: int,
    ) -> bool:
        """Claim the thread; False if a live lease is already there."""
        item: Dict[str, Any] = {
            "thread_id": thread_id,
            "run_id": run_id,
            "holder": holder,
            "lease_until": now_ms + lease_ms,
            "expires_at": (now_ms + lease_ms) // 1000 + _TTL_GRACE_SECONDS,
        }
        if owner_sub:
            item["owner_sub"] = owner_sub
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(thread_id) OR lease_until < :now",
                ExpressionAttributeValues={":now": now_ms},
            )
            return True
        except ClientError as exc:
            if _conditional_failed(exc):
                return False
            raise

    def renew(self, thread_id: str, run_id: str, now_ms: int, lease_ms: int) -> Optional[Dict[str, Any]]:
        """Extend this run's lease; the row as it now is, or None if it is not ours."""
        try:
            resp = self.table.update_item(
                Key={"thread_id": thread_id},
                UpdateExpression="SET lease_until = :until, expires_at = :expires",
                ConditionExpression="run_id = :run",
                ExpressionAttributeValues={
                    ":until": now_ms + lease_ms,
                    ":expires": (now_ms + lease_ms) // 1000 + _TTL_GRACE_SECONDS,
                    ":run": run_id,
                },
                ReturnValues="ALL_NEW",
            )
            return resp.get("Attributes") or {}
        except ClientError as exc:
            if _conditional_failed(exc):
                return None
            raise

    def release(self, thread_id: str, run_id: str) -> None:
        """Drop this run's row. A row that is already gone, or a successor's, stays."""
        try:
            self.table.delete_item(
                Key={"thread_id": thread_id},
                ConditionExpression="run_id = :run",
                ExpressionAttributeValues={":run": run_id},
            )
        except ClientError as exc:
            if not _conditional_failed(exc):
                raise

    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        # Consistent: a reattach right after a start must see the claim.
        return self.table.get_item(Key={"thread_id": thread_id}, ConsistentRead=True).get("Item")

    def request_cancel(self, thread_id: str, owner_sub: Optional[str], now_ms: int) -> bool:
        """Flag a live run for its holder to stop; only its starter may."""
        values: Dict[str, Any] = {":now": now_ms, ":yes": True}
        condition = "attribute_exists(thread_id) AND lease_until >= :now"
        if owner_sub:
            condition += " AND (attribute_not_exists(owner_sub) OR owner_sub = :sub)"
            values[":sub"] = owner_sub
        try:
            self.table.update_item(
                Key={"thread_id": thread_id},
                UpdateExpression="SET cancel_requested = :yes",
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as exc:
            if _conditional_failed(exc):
                return False
            raise
