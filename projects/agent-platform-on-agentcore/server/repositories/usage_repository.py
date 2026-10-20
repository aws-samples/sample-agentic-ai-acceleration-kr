"""Usage rollup repository.

Counters only. Every mutation is an `ADD` so concurrent turns cannot lose each
other's increments, and every read is a keyed `Query` — the table exists to
replace a Scan over the threads table, so no access path here may be a Scan.

Keys are month-sharded (`AGENTS#2026-08`) and the enumerated dimension lives in
the sort key (`D#2026-08-15#A#rec-1`). That is the whole trick: DynamoDB requires
an exact partition key, so putting the agent or user id in `pk` would make
"list every agent for this month" unanswerable without a Scan.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from repositories.base import DynamoDBRepository

# Highest code point, so `sk BETWEEN "D#start" AND "D#end￿"` includes every
# suffix of the last day (`#A#rec-1`, `#U#sub`, …) without knowing them.
SK_MAX = "￿"

# Split points for reading one big partition as concurrent sort-key ranges. A
# turn event's key is `T#<thread uuid>:<message id>`, so its first character is
# a hex digit spread evenly across 16 ranges. The outer two ranges catch any key
# that does not start with a hex digit; `between` is inclusive, so a key equal
# to a split point would be read twice — the merge dedupes by sort key.
_SPLITS = list("123456789abcdef")



def month_shard(day: str) -> str:
    """`"2026-08-15"` -> `"2026-08"`."""
    return day[:7]


def _day_ranges(start_date: str, end_date: str, parts: int) -> List[tuple]:
    """Split an inclusive date window into at most `parts` contiguous
    `(first_day, last_day)` ranges of near-equal length, in order. A window
    shorter than `parts` days is one range per day."""
    try:
        first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        return [(start_date, end_date)]
    days = (last - first).days + 1
    if days <= 1 or parts <= 1:
        return [(start_date, end_date)]
    parts = min(parts, days)
    size, extra = divmod(days, parts)
    ranges: List[tuple] = []
    cursor = first
    for index in range(parts):
        length = size + (1 if index < extra else 0)
        stop = cursor + timedelta(days=length - 1)
        ranges.append((cursor.isoformat(), stop.isoformat()))
        cursor = stop + timedelta(days=1)
    return ranges


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

    def query(
        self,
        pk: str,
        start_date: str,
        end_date: str,
        fields: Optional[Sequence[str]] = None,
        ranges: int = 1,
    ) -> List[Dict[str, Any]]:
        """Every counter in one partition between two dates, inclusive.

        `fields` returns only those attributes (plus `sk`). It does not lower the
        read cost, but it is most of this process's CPU: a row carries a dozen
        Number counters, each decoded to a Decimal, and the per-agent user rows
        `distinct_users` counts were 54k of them per summary at 1,000 people.

        No filter on the sort key: DynamoDB rejects a `FilterExpression` over a
        key attribute (ValidationException, seen live 2026-10-19), so one
        record's rows out of `D#<day>#A#<record>` are read by exact key instead
        (`get_many`, see `UsageService._record_rows`).

        `ranges` > 1 reads the window as that many contiguous day ranges at
        once (they partition it, so the merge is a plain concatenation). For a
        wide roll-up partition — a 30-day month of AGENTS# at 300 agents is a
        few MB, three or four 1 MB pages that used to be followed one after
        another. Not for a per-record partition of a few rows: there it only
        multiplies round trips, and the summary's per-agent prefetch made 2,500
        Queries instead of 600 when every read was split.
        """
        base: Dict[str, Any] = {}
        if fields:
            names = {f"#p{i}": name for i, name in enumerate(dict.fromkeys(["sk", *fields]))}
            base["ProjectionExpression"] = ", ".join(names)
            base["ExpressionAttributeNames"] = names

        def read(bounds: tuple) -> List[Dict[str, Any]]:
            low, high = bounds
            return self._query_all(
                KeyConditionExpression=Key("pk").eq(pk)
                & Key("sk").between(f"D#{low}", f"D#{high}{SK_MAX}"),
                **dict(base),
            )

        bounds = _day_ranges(start_date, end_date, ranges)
        if len(bounds) == 1:
            return read(bounds[0])
        with ThreadPoolExecutor(max_workers=len(bounds)) as pool:
            pages = list(pool.map(read, bounds))
        return [item for page in pages for item in page]

    def query_prefix(self, pk: str, sk_prefix: str) -> List[Dict[str, Any]]:
        """Every item in one partition whose sort key starts with `sk_prefix`."""
        return self._query_all(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(sk_prefix)
        )

    def get_many(self, keys: Sequence[tuple]) -> List[Dict[str, Any]]:
        """Items for exact (pk, sk) keys, 100 per BatchGetItem, retrying
        UnprocessedKeys. Absent keys are simply not returned."""
        out: List[Dict[str, Any]] = []
        name = self.table.name
        unique = list(dict.fromkeys(keys))
        for start in range(0, len(unique), 100):
            request = {name: {"Keys": [{"pk": pk, "sk": sk} for pk, sk in unique[start:start + 100]]}}
            while request:
                response = self.dynamodb.batch_get_item(RequestItems=request)
                out.extend(response.get("Responses", {}).get(name, []))
                request = response.get("UnprocessedKeys") or {}
        return out

    def query_prefix_split(
        self,
        pk: str,
        sk_prefix: str,
        filter_expression: Any = None,
        fields: Optional[Sequence[str]] = None,
        workers: int = 16,
    ) -> List[Dict[str, Any]]:
        """`query_prefix` for a partition too big to read one page at a time.

        A month of turn events is one partition (`TURNS#2026-10`), ~70k items at
        1,000 users; read sequentially it was 70 pages and the per-record turn
        list took 53 s (bst load test, 2026-10-18). This reads 17 sort-key ranges
        concurrently, and lets DynamoDB drop non-matching items
        (`filter_expression`) and unneeded attributes (`fields`) before they reach
        this process, which was the CPU-bound half of the cost.
        """
        bounds = [sk_prefix] + [sk_prefix + c for c in _SPLITS] + [sk_prefix + SK_MAX]
        ranges = list(zip(bounds[:-1], bounds[1:]))
        base: Dict[str, Any] = {}
        if filter_expression is not None:
            base["FilterExpression"] = filter_expression
        if fields:
            names = {f"#p{i}": name for i, name in enumerate(dict.fromkeys(["sk", *fields]))}
            base["ProjectionExpression"] = ", ".join(names)
            base["ExpressionAttributeNames"] = names

        def read(bounds_pair):
            low, high = bounds_pair
            return self._query_all(
                KeyConditionExpression=Key("pk").eq(pk) & Key("sk").between(low, high), **dict(base)
            )

        with ThreadPoolExecutor(max_workers=min(workers, len(ranges))) as pool:
            pages = list(pool.map(read, ranges))
        seen: Dict[str, Dict[str, Any]] = {}
        for page in pages:
            for item in page:
                seen.setdefault(str(item.get("sk")), item)
        return list(seen.values())
