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
    table = StubTable()
    items = repo(table).query("P", "2026-09-01", "2026-09-30")
    assert len(items) == 2


def test_floats_are_stored_as_decimals():
    """DynamoDB rejects Python floats outright; a reconciliation row carrying a
    `billed_1k_tokens` float used to fail its whole SET silently."""
    table = StubTable()
    repo(table).set_fields("P", "S", {"ratio": 0.25, "count": 3})
    values = table.calls[0][1]["ExpressionAttributeValues"]
    from decimal import Decimal
    assert Decimal("0.25") in values.values()
    assert all(not isinstance(v, float) for v in values.values())

    table = StubTable()
    repo(table).put_if_absent("P", "S", {"quantity": 1.5, "nested": {"x": 2.5}, "list": [0.5]})
    item = table.calls[0][1]["Item"]
    assert item["quantity"] == Decimal("1.5")
    assert item["nested"]["x"] == Decimal("2.5")
    assert item["list"][0] == Decimal("0.5")


def test_delete_removes_one_item_by_key():
    table = StubTable()
    repo(table).delete("P", "S")
    assert table.calls[0] == ("delete_item", {"Key": {"pk": "P", "sk": "S"}})
