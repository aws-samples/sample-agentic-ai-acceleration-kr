"""Team settings: rows in the teams table."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import AuthUser  # noqa: E402
from models.team import TeamConfigUpdate  # noqa: E402
from services.team_service import TeamService, TeamStorageUnavailable  # noqa: E402
from team_table_stub import Table, seed  # noqa: E402

ROLES = {"finance": "arn:aws:iam::1:role/bap-harness-finance", "hr": "arn:aws:iam::1:role/bap-harness-hr"}


def test_put_then_get_round_trips_through_the_team_row():
    table = seed(Table(), ROLES)
    svc = TeamService(repository=table)
    out = svc.put("finance", TeamConfigUpdate(label="재무팀", allowed_models=["m1"], allowed_tools=["@builtin"], daily_cost_alert_usd=20), by="admin")
    assert out.label == "재무팀" and out.execution_role_arn == ROLES["finance"]
    row = table.rows["finance"]
    assert row["allowed_tools"] == ["@builtin"] and row["updated_by"] == "admin"
    assert row["execution_role_arn"] == ROLES["finance"] and row["managed_by"] == "platform"
    assert svc.get("finance").daily_cost_alert_usd == 20
    assert [t.name for t in svc.list()] == ["finance", "hr"]


def test_unknown_team_is_refused_and_unconfigured_storage_has_no_teams():
    svc = TeamService(repository=seed(Table(), ROLES))
    assert svc.role_for("hr") == ROLES["hr"] and svc.role_for("nope") == ""
    with pytest.raises(ValueError):
        svc.put("nope", TeamConfigUpdate(label="x"))
    none = TeamService(repository=None)
    assert none.configured is False and none.enabled is False and none.list() == []
    with pytest.raises(TeamStorageUnavailable):
        none.put("finance", TeamConfigUpdate(label="x"))


def test_for_user_and_model_allowance():
    svc = TeamService(repository=seed(Table(), ROLES))
    svc.put("finance", TeamConfigUpdate(allowed_models=["m1", "m2"]))
    svc.put("hr", TeamConfigUpdate(allowed_models=[]))
    user = AuthUser(username="u", groups=["team:finance", "team:ghost"])
    assert [t.name for t in svc.for_user(user)] == ["finance"]
    assert svc.allowed_models_for(["finance"]) == {"m1", "m2"}
    assert svc.allowed_models_for(["finance", "hr"]) is None  # a team without a list lifts the limit
    assert svc.allowed_models_for([]) is None
    assert svc.allowed_models_for(["ghost"]) is None


def test_failing_storage_lists_nothing_and_put_refuses_and_writes_nothing():
    table = Table(fail=True)
    svc = TeamService(repository=table)
    assert svc.list() == [] and svc.enabled is False
    with pytest.raises(TeamStorageUnavailable):
        svc.put("finance", TeamConfigUpdate(label="x"))
    assert table.rows == {}


def test_failed_write_is_storage_unavailable_and_leaves_the_stored_row_alone():
    class WriteFails(Table):
        broken = False

        def put_team(self, item):
            if self.broken:
                raise RuntimeError("throttled")
            super().put_team(item)

    table = seed(WriteFails(), ROLES)
    svc = TeamService(repository=table)
    table.broken = True
    with pytest.raises(TeamStorageUnavailable):
        svc.put("finance", TeamConfigUpdate(label="x"))
    assert svc.get("finance").label == "finance"


def test_cost_alert_can_be_cleared_by_an_explicit_null_and_omitted_fields_stay():
    svc = TeamService(repository=seed(Table(), ROLES))
    svc.put("finance", TeamConfigUpdate(daily_cost_alert_usd=50, label="재무"))
    assert svc.get("finance").daily_cost_alert_usd == 50
    svc.put("finance", TeamConfigUpdate(allowed_models=["m1"]))          # alert omitted: kept
    assert svc.get("finance").daily_cost_alert_usd == 50
    svc.put("finance", TeamConfigUpdate.model_validate({"daily_cost_alert_usd": None}))
    team = svc.get("finance")
    assert team.daily_cost_alert_usd is None and team.label == "재무" and team.allowed_models == ["m1"]


def test_names_are_the_rows():
    assert TeamService(seed(Table(), ROLES)).names() == {"finance", "hr"}
    assert TeamService(Table()).names() == set()
    assert TeamService(None).names() == set()


def test_external_rows_are_read_as_they_are():
    # A row another stack wrote (managed_by != platform) keeps its role and label.
    table = seed(Table(), {"ops": "arn:aws:iam::1:role/ws-harness-ops"}, managed_by="workshop")
    svc = TeamService(table)
    team = svc.get("ops")
    assert team.managed_by == "workshop" and team.execution_role_arn.endswith("ws-harness-ops")
    assert svc.role_for("ops").endswith("ws-harness-ops")
    svc.put("ops", TeamConfigUpdate(label="운영"))
    assert table.rows["ops"]["managed_by"] == "workshop" and table.rows["ops"]["label"] == "운영"
