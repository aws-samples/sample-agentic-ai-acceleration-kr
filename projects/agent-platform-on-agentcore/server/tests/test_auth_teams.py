"""Teams are Cognito groups with the `team:` prefix; nothing else is a team."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.auth as auth_routes  # noqa: E402
from core.auth import TEAM_GROUP_PREFIX, AuthUser  # noqa: E402


def test_teams_are_prefixed_groups_in_order_without_duplicates():
    user = AuthUser(username="u", groups=["user", "team:finance", "admin", "team:hr", "team:finance"])
    assert TEAM_GROUP_PREFIX == "team:"
    assert user.teams == ["finance", "hr"]


def test_no_team_groups_means_no_teams():
    assert AuthUser(username="u", groups=["user", "admin"]).teams == []
    assert AuthUser(username="u").teams == []


def test_session_profile_carries_teams():
    profile = auth_routes.session(AuthUser(sub="s", username="u", groups=["team:support"], provider="cognito"))
    assert profile.teams == ["support"]
    assert profile.role == "user"
