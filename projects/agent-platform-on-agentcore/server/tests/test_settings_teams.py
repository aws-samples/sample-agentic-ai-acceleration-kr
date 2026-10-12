"""`/api/settings/teams`: admins edit every team, a person reads their own."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import routes.settings as settings  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from services.team_service import TeamService  # noqa: E402
from team_table_stub import Table, seed  # noqa: E402

ROLES = {"finance": "arn:aws:iam::1:role/f", "hr": "arn:aws:iam::1:role/h"}


def teardown_function():
    settings._teams_override = None


def admin():
    return AuthUser(sub="a", username="admin", groups=["admin"])


def member(*teams):
    return AuthUser(sub="m", username="member", groups=[f"team:{t}" for t in teams])


def test_admin_sees_every_team_with_role_arn_and_member_sees_only_their_own_without_it():
    settings._teams_override = TeamService(seed(Table(), ROLES))
    everyone = settings.get_teams(admin())
    assert [t["name"] for t in everyone["teams"]] == ["finance", "hr"]
    assert everyone["teams"][0]["execution_role_arn"] == ROLES["finance"]
    mine = settings.get_teams(member("hr"))
    assert [t["name"] for t in mine["teams"]] == ["hr"]
    assert mine["teams"][0]["execution_role_arn"] == ""
