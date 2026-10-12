"""An Entra user's teams come from the token's group ids through the team mapping."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402


def teardown_function():
    auth.entra_team_resolver = None


def test_mapped_entra_groups_become_team_groups_without_duplicates():
    auth.entra_team_resolver = lambda gids: ["hr"] if "g-hr" in gids else []
    groups = auth.with_entra_teams(["g-hr", "g-x", "team:hr"])
    assert groups == ["g-hr", "g-x", "team:hr"]
    groups = auth.with_entra_teams(["g-hr"])
    assert groups == ["g-hr", "team:hr"]
    assert auth.AuthUser(username="u", groups=groups).teams == ["hr"]


def test_no_resolver_or_resolver_failure_leaves_groups_alone():
    assert auth.with_entra_teams(["g-hr"]) == ["g-hr"]

    def boom(gids):
        raise RuntimeError("table down")

    auth.entra_team_resolver = boom
    assert auth.with_entra_teams(["g-hr"]) == ["g-hr"]
