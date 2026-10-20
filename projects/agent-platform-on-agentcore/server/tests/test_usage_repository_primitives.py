"""The three write shapes the ledger needs, and paginated reads.

`put_if_absent` is the idempotency of the turn event: a second flush of the same
turn must see False and leave the derived counters alone. `set_fields` is the
collector's idempotency: a day re-read from CloudWatch overwrites, never adds.
`query`/`query_prefix` follow `LastEvaluatedKey` because a month partition past
1 MB silently shortened every total before.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from botocore.exceptions import ClientError  # noqa: E402

from repositories.usage_repository import UsageRepository  # noqa: E402


class StubTable:
    def __init__(self, existing=False):
        self.existing = existing
        self.calls = []

    def put_item(self, **kwargs):
        self.calls.append(("put_item", kwargs))
        if self.existing:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )

    def update_item(self, **kwargs):
        self.calls.append(("update_item", kwargs))

    def delete_item(self, **kwargs):
        self.calls.append(("delete_item", kwargs))

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        return {"Item": {"pk": kwargs["Key"]["pk"], "sk": kwargs["Key"]["sk"], "x": 1}}

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        if "ExclusiveStartKey" not in kwargs:
            return {"Items": [{"sk": "D#2026-09-01#A#a"}], "LastEvaluatedKey": {"pk": "p", "sk": "a"}}
        return {"Items": [{"sk": "D#2026-09-02#A#b"}]}


def repo(table):
    instance = UsageRepository.__new__(UsageRepository)
    instance.table = table
    return instance


def test_put_if_absent_uses_condition_and_reports_conflict():
    table = StubTable()
    assert repo(table).put_if_absent("P", "S", {"a": 1}) is True
    kwargs = table.calls[0][1]
    assert kwargs["ConditionExpression"] == "attribute_not_exists(pk)"
    assert kwargs["Item"] == {"pk": "P", "sk": "S", "a": 1}

    assert repo(StubTable(existing=True)).put_if_absent("P", "S", {"a": 1}) is False


def test_set_fields_only_sets():
    table = StubTable()
    repo(table).set_fields("P", "S", {"turns": 5, "note": "x"})
    kwargs = table.calls[0][1]
    assert kwargs["UpdateExpression"].startswith("SET ")
    assert "ADD" not in kwargs["UpdateExpression"]
    assert set(kwargs["ExpressionAttributeNames"].values()) == {"turns", "note"}
    assert set(kwargs["ExpressionAttributeValues"].values()) == {5, "x"}


def test_set_fields_with_nothing_writes_nothing():
    table = StubTable()
    repo(table).set_fields("P", "S", {})
    assert table.calls == []


def test_get_returns_the_item():
    assert repo(StubTable()).get("P", "S")["x"] == 1


def test_query_prefix_paginates():
    table = StubTable()
    items = repo(table).query_prefix("P", "D#2026-09")
    assert [item["sk"] for item in items] == ["D#2026-09-01#A#a", "D#2026-09-02#A#b"]
    assert "ExclusiveStartKey" in table.calls[1][1]


def test_query_paginates_too():
    # One day, so one range: the stub answers every Query with the same two pages.
    table = StubTable()
    items = repo(table).query("P", "2026-09-01", "2026-09-01")
    assert len(items) == 2
    assert "ExclusiveStartKey" in table.calls[1][1]


class RangeTable:
    """Answers each Query with the days its key range covers, and records the ranges."""

    def __init__(self):
        self.ranges = []
        self.filters = []

    def query(self, **kwargs):
        cond = kwargs["KeyConditionExpression"]
        # `Key("pk").eq(pk) & Key("sk").between(low, high)`
        between = cond._values[1]
        low, high = between._values[1], between._values[2]
        self.ranges.append((low, high))
        self.filters.append(kwargs.get("FilterExpression"))
        assert "FilterExpression" not in kwargs, "a filter on the key attribute is rejected by DynamoDB"
        return {"Items": [{"sk": f"{low}#A#x"}]}


def test_query_reads_a_window_as_disjoint_concurrent_day_ranges():
    """A 30-day month partition at 300 agents is several MB and several pages;
    read as one sequential Query it was the per-record Usage page's floor.
    The window is split into contiguous day ranges that together cover it
    exactly once, so the merged result needs no dedupe."""
    table = RangeTable()
    items = repo(table).query("P", "2026-09-01", "2026-09-30", ranges=4)
    assert 1 < len(table.ranges) <= 4
    lows = sorted(low for low, _ in table.ranges)
    assert lows[0] == "D#2026-09-01"
    highs = sorted(high for _, high in table.ranges)
    assert highs[-1] == "D#2026-09-30" + "\uffff"
    # Contiguous: each range's low is the day after the previous range's high.
    from datetime import date, timedelta
    ordered = sorted(table.ranges)
    for (_, high), (low, _) in zip(ordered, ordered[1:]):
        prev_day = date.fromisoformat(high[2:12])
        assert low == f"D#{(prev_day + timedelta(days=1)).isoformat()}"
    assert len(items) == len(table.ranges)
    assert all(f is None for f in table.filters)


def test_query_for_one_day_is_one_range():
    table = RangeTable()
    repo(table).query("P", "2026-09-07", "2026-09-07", ranges=4)
    assert table.ranges == [("D#2026-09-07", "D#2026-09-07\uffff")]


def test_query_is_one_range_unless_asked():
    """A per-record partition is a few rows; splitting it only multiplies calls."""
    table = RangeTable()
    repo(table).query("P", "2026-09-01", "2026-09-30")
    assert table.ranges == [("D#2026-09-01", "D#2026-09-30\uffff")]
