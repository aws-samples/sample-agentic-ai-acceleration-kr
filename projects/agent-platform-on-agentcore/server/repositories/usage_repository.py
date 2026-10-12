"""Usage rollup repository.

Counters only. Every mutation is an `ADD` so concurrent turns cannot lose each
other's increments, and every read is a keyed `Query` — the table exists to
replace a Scan over the threads table, so no access path here may be a Scan.

Keys are month-sharded (`AGENTS#2026-08`) and the enumerated dimension lives in
the sort key (`D#2026-08-15#A#rec-1`). That is the whole trick: DynamoDB requires
an exact partition key, so putting the agent or user id in `pk` would make
"list every agent for this month" unanswerable without a Scan.
"""
from decimal import Decimal
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from repositories.base import DynamoDBRepository

# Highest code point, so `sk BETWEEN "D#start" AND "D#end￿"` includes every
# suffix of the last day (`#A#rec-1`, `#U#sub`, …) without knowing them.
SK_MAX = "￿"


def month_shard(date: str) -> str:
    """`"2026-08-15"` -> `"2026-08"`."""
    return date[:7]


def _dynamo_safe(value: Any) -> Any:
    """Floats become Decimals, recursively.

    DynamoDB rejects a Python float outright, and boto3's resource layer raises
    on it — so one float in a reconciliation row (`billed_1k_tokens`) made the
    whole SET fail, and the failure was a logged warning nobody saw. Converting
    at the repository boundary means no caller has to remember.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _dynamo_safe(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamo_safe(inner) for inner in value]
    return value


class UsageRepository(DynamoDBRepository):
    """Counter access for the usage rollup table."""

    def add(
        self,
        pk: str,
        sk: str,
        counters: Dict[str, int],
        flags: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Accumulate `counters` onto one item, and set any `flags`.

        Zero counters are dropped rather than written. An absent token attribute
        is how the read side tells "not measured" from "measured as zero", and a
        stored 0 would make a backfilled day look free.
        """
        increments = {name: value for name, value in counters.items() if value}
        if not increments and not flags:
            return

        names: Dict[str, str] = {}
        values: Dict[str, Any] = {}
        add_parts = []
        for index, (name, value) in enumerate(increments.items()):
            names[f"#c{index}"] = name
            values[f":c{index}"] = value
            add_parts.append(f"#c{index} :c{index}")

        set_parts = []
        for index, (name, value) in enumerate((flags or {}).items()):
            names[f"#f{index}"] = name
            values[f":f{index}"] = value
            set_parts.append(f"#f{index} = :f{index}")

        clauses = []
        if add_parts:
            clauses.append("ADD " + ", ".join(add_parts))
        if set_parts:
            clauses.append("SET " + ", ".join(set_parts))

        self.table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression=" ".join(clauses),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def put_if_absent(self, pk: str, sk: str, item: Dict[str, Any]) -> bool:
        """Write an item once. False when the key already exists.

        This is the turn event's idempotency: a turn that flushes twice (once at
        `messageStop`, once post-loop) must be counted once, so the caller treats
        False as "already counted" and folds the second flush's tokens onto the
        existing event instead of ADDing a second `turns`.
        """
        try:
            self.table.put_item(
                Item={"pk": pk, "sk": sk, **_dynamo_safe(item)},
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def set_fields(self, pk: str, sk: str, fields: Dict[str, Any]) -> None:
        """Overwrite named attributes on one item, creating it if needed.

        `SET` only, never `ADD`: the collector re-reads a day from CloudWatch on
        every pass and writes what it read, so running twice cannot double a
        quantity. Nothing to write is a no-op rather than an empty expression,
        which DynamoDB rejects.
        """
        if not fields:
            return
        names = {f"#f{index}": name for index, name in enumerate(fields)}
        values = {f":f{index}": _dynamo_safe(value) for index, value in enumerate(fields.values())}
        self.table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET "
            + ", ".join(f"#f{index} = :f{index}" for index in range(len(fields))),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def get(self, pk: str, sk: str) -> Optional[Dict[str, Any]]:
        """One item, or None."""
        return self.table.get_item(Key={"pk": pk, "sk": sk}).get("Item")

    def delete(self, pk: str, sk: str) -> None:
        """Remove one item. Only the backfill's key migration uses this."""
        self.table.delete_item(Key={"pk": pk, "sk": sk})

    def _query_all(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """A keyed Query, following `LastEvaluatedKey` to the end.

        A month partition past 1 MB used to return its first page only, and every
        total built on it was short by the rest with nothing to show the hole.
        """
        items: List[Dict[str, Any]] = []
        while True:
            page = self.table.query(**kwargs)
            items.extend(page.get("Items", []))
            last = page.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last

    def query(self, pk: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """Every counter in one partition between two dates, inclusive."""
        return self._query_all(
            KeyConditionExpression=Key("pk").eq(pk)
            & Key("sk").between(f"D#{start_date}", f"D#{end_date}{SK_MAX}")
        )

    def query_prefix(self, pk: str, sk_prefix: str) -> List[Dict[str, Any]]:
        """Every item in one partition whose sort key starts with `sk_prefix`."""
        return self._query_all(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(sk_prefix)
        )
