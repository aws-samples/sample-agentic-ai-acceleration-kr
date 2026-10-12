"""Who sees a record: admins everything, others shared + their own teams."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import AuthUser  # noqa: E402
from services.team_access import SHARED, can_see, filter_visible, normalize_team, team_of  # noqa: E402


def member(*teams):
    return AuthUser(username="m", groups=[f"team:{t}" for t in teams])


def test_normalize_treats_blank_shared_and_non_strings_as_no_team():
    assert normalize_team(None) is None and normalize_team("") is None and normalize_team(SHARED) is None
    assert normalize_team(True) is None and normalize_team(3) is None
    assert normalize_team(" Finance ") == "finance"


def test_team_of_reads_custom_metadata():
    assert team_of(SimpleNamespace(custom_metadata={"team": "HR", "tier": "internal"})) == "hr"
    assert team_of(SimpleNamespace(custom_metadata=None)) is None
    assert team_of(SimpleNamespace()) is None


def test_visibility_rules():
    admin = AuthUser(username="a", groups=["admin"])
    on = {"teams_enabled": True}
    assert can_see("finance", admin, **on)
    assert can_see("finance", None, **on)          # no caller = internal call, nothing to hide from
    assert can_see(None, member(), **on)           # shared is visible to everyone
    assert can_see("finance", member("finance", "hr"), **on)
    assert not can_see("finance", member("hr"), **on)
    assert not can_see("finance", member(), **on)
    # A value that is not a declared team stays hidden from non-members.
    assert not can_see("platform", member("hr"), **on)


def test_no_teams_declared_means_everything_is_visible():
    # The schema's free-string `team` predates teams: a label must not hide records.
    assert can_see("search", member(), teams_enabled=False)
    assert can_see("finance", member("hr"), teams_enabled=False, known=False)


def test_unknown_visibility_fails_closed_for_non_admins_only():
    admin = AuthUser(username="a", groups=["admin"])
    assert not can_see(None, member("hr"), teams_enabled=True, known=False)
    assert not can_see(None, member(), teams_enabled=True, known=False)
    assert can_see(None, admin, teams_enabled=True, known=False)
    assert can_see(None, None, teams_enabled=True, known=False)


def test_filter_keeps_order_and_reads_metadata():
    records = [
        SimpleNamespace(custom_metadata=None),
        SimpleNamespace(custom_metadata={"team": "hr"}),
        SimpleNamespace(custom_metadata={"team": "finance"}),
    ]
    assert [team_of(r) for r in filter_visible(records, member("finance"), teams_enabled=True)] == [None, "finance"]
    assert len(filter_visible(records, AuthUser(username="a", groups=["admin"]), teams_enabled=True)) == 3
    assert len(filter_visible(records, member(), teams_enabled=False)) == 3


def test_filter_hides_records_whose_visibility_is_unknown():
    records = [
        SimpleNamespace(custom_metadata=None, visibility_known=True),
        SimpleNamespace(custom_metadata=None, visibility_known=False),
    ]
    assert len(filter_visible(records, member("hr"), teams_enabled=True)) == 1
    assert len(filter_visible(records, member("hr"), teams_enabled=False)) == 2
