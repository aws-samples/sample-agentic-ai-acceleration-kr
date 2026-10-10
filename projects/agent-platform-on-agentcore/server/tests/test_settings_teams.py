"""`/api/settings/teams`: admins edit every team, a person reads their own."""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import core.config as cfg  # noqa: E402
import routes.settings as settings  # noqa: E402
from core.auth import AuthUser, require_admin  # noqa: E402
from models.team import TeamConfigUpdate  # noqa: E402
from services.team_service import TeamService  # noqa: E402

ROLES = {"finance": "arn:aws:iam::1:role/f", "hr": "arn:aws:iam::1:role/h"}


class Prefs:
    def __init__(self):
        self.items = {}

    def get(self, sub, name):
        return self.items.get((sub, name))

    def put(self, sub, name, value):
        self.items[(sub, name)] = value


def teardown_function():
    settings._teams_override = None


def admin():
    return AuthUser(sub="a", username="admin", groups=["admin"])


def member(*teams):
    return AuthUser(sub="m", username="member", groups=[f"team:{t}" for t in teams])


def test_admin_sees_every_team_with_role_arn_and_member_sees_only_their_own_without_it():
    settings._teams_override = TeamService(Prefs(), ROLES)
    everyone = settings.get_teams(admin())
    assert [t["name"] for t in everyone["teams"]] == ["finance", "hr"]
    assert everyone["teams"][0]["execution_role_arn"] == ROLES["finance"]
    mine = settings.get_teams(member("hr"))
    assert [t["name"] for t in mine["teams"]] == ["hr"]
    assert mine["teams"][0]["execution_role_arn"] == ""


def test_put_updates_only_editable_fields_and_404s_unknown_team():
    settings._teams_override = TeamService(Prefs(), ROLES)
    out = settings.put_team("finance", TeamConfigUpdate(label="재무팀", allowed_models=["m1"]), user=admin())
    assert out.label == "재무팀" and out.allowed_models == ["m1"] and out.execution_role_arn == ROLES["finance"]
    with pytest.raises(HTTPException) as caught:
        settings.put_team("ghost", TeamConfigUpdate(label="x"), user=admin())
    assert caught.value.status_code == 404


def test_put_is_503_when_storage_cannot_be_read():
    class Down(Prefs):
        def get(self, sub, name):
            raise RuntimeError("table down")

    prefs = Down()
    settings._teams_override = TeamService(prefs, ROLES)
    with pytest.raises(HTTPException) as caught:
        settings.put_team("finance", TeamConfigUpdate(label="x"), user=admin())
    assert caught.value.status_code == 503
    assert prefs.items == {}


def test_put_is_503_when_storage_cannot_be_written():
    class WriteDown(Prefs):
        def put(self, sub, name, value):
            raise RuntimeError("throttled")

    settings._teams_override = TeamService(WriteDown(), ROLES)
    with pytest.raises(HTTPException) as caught:
        settings.put_team("finance", TeamConfigUpdate(label="x"), user=admin())
    assert caught.value.status_code == 503


def test_write_is_admin_only():
    deps = [p.default.dependency for p in inspect.signature(settings.put_team).parameters.values() if hasattr(p.default, "dependency")]
    assert require_admin in deps


def test_json_dict_env_is_forgiving(monkeypatch):
    monkeypatch.setenv("X_ROLES", '{"a": "arn:a", "b": 3, "c": ""}')
    assert cfg._json_dict("X_ROLES") == {"a": "arn:a"}
    monkeypatch.setenv("X_ROLES", "not json")
    assert cfg._json_dict("X_ROLES") == {}
    monkeypatch.delenv("X_ROLES")
    assert cfg._json_dict("X_ROLES") == {}
