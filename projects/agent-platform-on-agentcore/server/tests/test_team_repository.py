"""TeamRepository key shapes, pagination and DynamoDB number handling, on a fake boto3 Table."""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from models.team import TeamConfigUpdate  # noqa: E402
from repositories.team_repository import TeamRepository  # noqa: E402
from services.team_service import TeamService  # noqa: E402


class FakeTable:
    def __init__(self, pages=None):
        self.items = {}
        self.pages = list(pages or [])
        self.queries = []

    def put_item(self, Item):
        for v in Item.values():
            assert not isinstance(v, float), "boto3 rejects floats"
        self.items[(Item["pk"], Item["sk"])] = Item

    def get_item(self, Key):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item else {}

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)

    def query(self, **kw):
        self.queries.append(kw)
        if self.pages:
            return self.pages.pop(0)
        return {"Items": [i for (pk, _), i in self.items.items() if pk == "TEAM"]}


def repo(table):
    r = TeamRepository.__new__(TeamRepository)
    r.table = table
    return r


def test_team_row_key_shape_strips_none_and_keys_on_name():
    t = FakeTable()
    r = repo(t)
    r.put_team({"name": "hr", "label": "인사", "error": None})
    row = t.items[("TEAM", "hr")]
    assert "error" not in row and row["label"] == "인사"
    assert r.get_team("hr") == {"name": "hr", "label": "인사"}
    assert r.get_team("ghost") is None
    r.delete_team("hr")
    assert r.get_team("hr") is None


def test_list_teams_follows_last_evaluated_key_and_strips_keys():
    t = FakeTable(pages=[
        {"Items": [{"pk": "TEAM", "sk": "a", "name": "a"}], "LastEvaluatedKey": {"pk": "TEAM", "sk": "a"}},
        {"Items": [{"pk": "TEAM", "sk": "b", "name": "b"}]},
    ])
    assert repo(t).list_teams() == [{"name": "a"}, {"name": "b"}]
    assert t.queries[1]["ExclusiveStartKey"] == {"pk": "TEAM", "sk": "a"}


def test_audit_key_shape_and_range_query():
    t = FakeTable(pages=[{"Items": [{"pk": "AUDIT#2026-10", "sk": "x", "id": "1", "at": "2026-10-10T00:00:00Z"}]}])
    r = repo(t)
    r.append_audit({"at": "2026-10-10T01:02:03.004Z", "id": "abc", "action": "create", "note": None})
    row = t.items[("AUDIT#2026-10", "2026-10-10T01:02:03.004Z#abc")]
    assert row["action"] == "create" and "note" not in row
    out = r.list_audit("2026-10", "2026-10-01T00:00:00Z", "2026-10-31T23:59:59Z")
    assert out == [{"id": "1", "at": "2026-10-10T00:00:00Z"}]
    q = t.queries[0]
    assert q["ScanIndexForward"] is False
    assert "AUDIT#2026-10" in repr(q["KeyConditionExpression"].get_expression()["values"][0].get_expression()["values"])


def test_cost_alert_float_is_stored_as_decimal_and_read_back_as_float():
    t = FakeTable()
    r = repo(t)
    svc = TeamService.__new__(TeamService)
    svc._repository, svc._entra_cache, svc._entra_cache_at = r, None, 0.0
    svc._clock = lambda: 0.0
    r.put_team({"name": "hr", "label": "hr", "managed_by": "platform"})
    svc.put("hr", TeamConfigUpdate(daily_cost_alert_usd=3.5))
    stored = t.items[("TEAM", "hr")]["daily_cost_alert_usd"]
    assert isinstance(stored, Decimal) and stored == Decimal("3.5")
    assert svc.get("hr").daily_cost_alert_usd == pytest.approx(3.5)
