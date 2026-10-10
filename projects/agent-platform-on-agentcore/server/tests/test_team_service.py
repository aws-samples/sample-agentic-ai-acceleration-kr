"""Team settings: one platform document, seeded from terraform's role map."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import AuthUser  # noqa: E402
from models.team import TeamConfigUpdate  # noqa: E402
from services.team_service import PLATFORM_SUB, TEAMS_NAME, TeamService, TeamStorageUnavailable, reconcile_teams  # noqa: E402

ROLES = {"finance": "arn:aws:iam::1:role/bap-harness-finance", "hr": "arn:aws:iam::1:role/bap-harness-hr"}


class Prefs:
    def __init__(self, fail=False):
        self.items = {}
        self.fail = fail

    def get(self, sub, name):
        if self.fail:
            raise RuntimeError("table down")
        return self.items.get((sub, name))

    def put(self, sub, name, value):
        if self.fail:
            raise RuntimeError("table down")
        self.items[(sub, name)] = value


def test_reconcile_seeds_every_role_and_keeps_role_from_env_not_document():
    stored = {"teams": {"finance": {"label": "재무", "execution_role_arn": "arn:stale", "allowed_models": ["m1"]}}}
    teams = reconcile_teams(stored, ROLES)
    assert set(teams) == {"finance", "hr"}
    assert teams["finance"].label == "재무"
    assert teams["finance"].execution_role_arn == ROLES["finance"]
    assert teams["finance"].allowed_models == ["m1"]
    assert teams["hr"].label == "hr" and teams["hr"].allowed_tools == []


def test_reconcile_drops_teams_terraform_no_longer_declares_and_tolerates_garbage():
    assert reconcile_teams({"teams": {"ghost": {}}}, ROLES).keys() == ROLES.keys()
    assert reconcile_teams("nonsense", ROLES).keys() == ROLES.keys()
    assert reconcile_teams(None, {}) == {}


def test_put_then_get_round_trips_under_the_platform_key():
    prefs = Prefs()
    svc = TeamService(repository=prefs, seeded_roles=ROLES)
    out = svc.put("finance", TeamConfigUpdate(label="재무팀", allowed_models=["m1"], allowed_tools=["@builtin"], daily_cost_alert_usd=20), by="admin")
    assert out.label == "재무팀" and out.execution_role_arn == ROLES["finance"]
    doc = prefs.items[(PLATFORM_SUB, TEAMS_NAME)]
    assert doc["teams"]["finance"]["allowed_tools"] == ["@builtin"] and doc["updated_by"] == "admin"
    assert svc.get("finance").daily_cost_alert_usd == 20
    assert [t.name for t in svc.list()] == ["finance", "hr"]


def test_unknown_team_is_refused_and_unconfigured_storage_still_lists_seeds():
    svc = TeamService(repository=None, seeded_roles=ROLES)
    assert svc.configured is False
    assert [t.name for t in svc.list()] == ["finance", "hr"]
    assert svc.role_for("hr") == ROLES["hr"] and svc.role_for("nope") == ""
    with pytest.raises(ValueError):
        svc.put("nope", TeamConfigUpdate(label="x"))


def test_for_user_and_model_allowance():
    prefs = Prefs()
    svc = TeamService(repository=prefs, seeded_roles=ROLES)
    svc.put("finance", TeamConfigUpdate(allowed_models=["m1", "m2"]))
    svc.put("hr", TeamConfigUpdate(allowed_models=[]))
    user = AuthUser(username="u", groups=["team:finance", "team:ghost"])
    assert [t.name for t in svc.for_user(user)] == ["finance"]
    assert svc.allowed_models_for(["finance"]) == {"m1", "m2"}
    assert svc.allowed_models_for(["finance", "hr"]) is None  # a team without a list lifts the limit
    assert svc.allowed_models_for([]) is None
    assert svc.allowed_models_for(["ghost"]) is None


def test_failing_storage_lists_seeds_but_put_refuses_and_writes_nothing():
    prefs = Prefs(fail=True)
    svc = TeamService(repository=prefs, seeded_roles=ROLES)
    assert [t.name for t in svc.list()] == ["finance", "hr"]
    with pytest.raises(TeamStorageUnavailable):
        svc.put("finance", TeamConfigUpdate(label="x"))
    assert prefs.items == {}


def test_failed_write_raises_storage_unavailable():
    # Final review M11: a swallowed write answered 200 ("저장됨") for a value
    # that was never kept.
    class WriteFails(Prefs):
        def put(self, sub, name, value):
            raise RuntimeError("throttled")

    svc = TeamService(repository=WriteFails(), seeded_roles=ROLES)
    with pytest.raises(TeamStorageUnavailable):
        svc.put("finance", TeamConfigUpdate(label="x"))


def test_cost_alert_can_be_cleared_by_an_explicit_null_and_omitted_fields_stay():
    svc = TeamService(repository=Prefs(), seeded_roles=ROLES)
    svc.put("finance", TeamConfigUpdate(daily_cost_alert_usd=50, label="재무"))
    assert svc.get("finance").daily_cost_alert_usd == 50
    svc.put("finance", TeamConfigUpdate(allowed_models=["m1"]))          # alert omitted: kept
    assert svc.get("finance").daily_cost_alert_usd == 50
    svc.put("finance", TeamConfigUpdate.model_validate({"daily_cost_alert_usd": None}))
    team = svc.get("finance")
    assert team.daily_cost_alert_usd is None and team.label == "재무" and team.allowed_models == ["m1"]


def test_names_are_the_declared_teams():
    assert TeamService(None, ROLES).names() == {"finance", "hr"}
    assert TeamService(None, {}).names() == set()
