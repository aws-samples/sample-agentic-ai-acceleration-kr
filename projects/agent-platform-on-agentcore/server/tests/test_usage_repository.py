"""The usage rollup must add, never overwrite, and must never need a Scan.

Counters are updated concurrently by every turn of every user, so a read-modify-
write would lose increments. And the whole reason this table exists is that
aggregating over the threads table is a Scan; a key design that puts the
enumerated dimension in the partition key would quietly bring the Scan back.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repositories.usage_repository import (  # noqa: E402
    SK_MAX,
    UsageRepository,
    month_shard,
)


class StubTable:
    """Records calls instead of talking to DynamoDB."""

    def __init__(self, items=None):
        self.updates = []
        self.queries = []
        self.items = items or []

    def load(self):
        return None

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"Items": self.items}

    def scan(self, **kwargs):
        raise AssertionError(
            "the usage table must never be scanned — replacing a Scan over the "
            "threads table is the only reason this table exists"
        )


def repo_with(table):
    repo = UsageRepository.__new__(UsageRepository)
    repo.table_name = "t"
    repo.table = table
    return repo


def test_month_shard_derives_the_partition_suffix():
    assert month_shard("2026-08-15") == "2026-08"


def test_add_uses_an_add_expression_not_a_put():
    table = StubTable()
    repo_with(table).add("AGENTS#2026-08", "D#2026-08-15#A#rec-1",
                         {"turns": 1, "input_tokens": 120})

    assert len(table.updates) == 1
    expression = table.updates[0]["UpdateExpression"]
    assert expression.startswith("ADD "), (
        f"counters must accumulate, got {expression!r} — a SET would lose every "
        "concurrent turn but the last"
    )
    assert table.updates[0]["Key"] == {
        "pk": "AGENTS#2026-08",
        "sk": "D#2026-08-15#A#rec-1",
    }


def test_add_skips_zero_counters_so_absent_can_mean_unknown():
    """Writing 0 tokens would make an unmeasured turn look free."""
    table = StubTable()
    repo_with(table).add("AGENTS#2026-08", "D#2026-08-15#A#rec-1",
                         {"turns": 1, "input_tokens": 0, "output_tokens": 0})

    names = table.updates[0]["ExpressionAttributeNames"].values()
    assert "turns" in names
    assert "input_tokens" not in names
    assert "output_tokens" not in names


def test_add_writes_nothing_when_every_counter_is_zero():
    table = StubTable()
    repo_with(table).add("AGENTS#2026-08", "D#2026-08-15#A#rec-1", {"turns": 0})

    assert table.updates == []


def test_flags_are_set_not_added():
    """`backfilled` is a marker, not a count."""
    table = StubTable()
    repo_with(table).add(
        "AGENTS#2026-08", "D#2026-08-15#A#rec-1", {"turns": 3},
        flags={"backfilled": True},
    )

    expression = table.updates[0]["UpdateExpression"]
    assert "ADD " in expression
    assert "SET " in expression
    assert True in table.updates[0]["ExpressionAttributeValues"].values()


def test_query_is_a_keyed_range_never_a_scan():
    """A string check on the condition would pass against the wrong shape."""
    table = StubTable(items=[{"pk": "AGENTS#2026-08", "sk": "D#2026-08-15#A#rec-1"}])
    result = repo_with(table).query("AGENTS#2026-08", "2026-08-01", "2026-08-31")

    assert result == table.items
    call = table.queries[0]
    assert "KeyConditionExpression" in call
    assert "FilterExpression" not in call, (
        "a filter means rows were fetched and then discarded — the range must be keyed"
    )

    expression = call["KeyConditionExpression"].get_expression()
    assert expression["operator"] == "AND"
    pk_condition, sk_condition = expression["values"]
    assert pk_condition.get_expression()["values"][1] == "AGENTS#2026-08"

    sk_expression = sk_condition.get_expression()
    assert sk_expression["operator"] == "BETWEEN"
    assert list(sk_expression["values"][1:]) == ["D#2026-08-01", f"D#2026-08-31{SK_MAX}"]
