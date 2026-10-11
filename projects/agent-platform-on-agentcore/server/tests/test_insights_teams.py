"""`/api/insights/teams`: per-team spend and denials, admin only, unattributed shown as a row."""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.insights as insights  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from models.team import TeamConfigUpdate  # noqa: E402
from services.team_service import TeamService  # noqa: E402
from team_table_stub import Table, seed  # noqa: E402
from services.usage_service import UsageService  # noqa: E402
from test_usage_team import StubRepo  # noqa: E402


class Collector:
    def policy_decisions(self, start, end):
        return {"gw-1": {
            "2026-10-09": {"allow_decisions": 4, "deny_decisions": 6, "deny_by_tool": {},
                           "by_mode": {"LOG_ONLY": {"allow": 4, "deny": 6}}},
            "2026-10-10": {"allow_decisions": 10, "deny_decisions": 3, "deny_by_tool": {"t___lookup_salary": 3},
                           "by_mode": {"ENFORCE": {"allow": 10, "deny": 2}, "LOG_ONLY": {"allow": 0, "deny": 1}}},
        }}


def teardown_function():
    insights._usage_override = None
    insights._teams_override = None
    insights._collector_override = None
    insights._org_directory_override = None


def wire():
    repo = StubRepo()
    usage = UsageService(repository=repo)
    usage.record_turn(agent_record_id="rec-1", owner_sub="s1", input_tokens=10, output_tokens=5, tool_calls={}, date="2026-10-10", turn_id="t1", team="finance")
    usage.record_turn(agent_record_id="rec-1", owner_sub="s2", input_tokens=10, output_tokens=5, tool_calls={}, date="2026-10-10", turn_id="t2")
    usage.record_policy_denial(agent_record_id="rec-1", owner_sub="s1", team="finance", tool_name="t___lookup_salary", date="2026-10-10")
    teams = TeamService(seed(Table(), {"finance": "arn:f", "hr": "arn:h"}))
    teams.put("finance", TeamConfigUpdate(label="재무팀"))
    insights._usage_override = usage
    insights._teams_override = teams
    insights._collector_override = Collector()
    insights._org_directory_override = Directory()


def test_plain_user_is_refused():
    wire()
    with pytest.raises(HTTPException) as caught:
        insights.team_insights(days=7, user=AuthUser(username="u"))
    assert caught.value.status_code == 403


def test_rows_cover_every_team_and_the_unattributed_remainder(monkeypatch):
    wire()
    monkeypatch.setattr(insights, "_require_configured", lambda: None)
    monkeypatch.setattr(insights, "_window", lambda days: {"days": days, "start_date": "2026-10-01", "end_date": "2026-10-31", "timezone": "UTC", "partial_day": False})
    out = insights.team_insights(days=7, user=AuthUser(username="a", groups=["admin"]))
    rows = {r["team"]: r for r in out["teams"]}
    assert rows["finance"]["label"] == "재무팀" and rows["finance"]["turns"] == 1
    assert rows["finance"]["policy_denials"] == 1 and rows["finance"]["denied_tools"] == {"t___lookup_salary": 1}
    assert rows["hr"]["turns"] == 0 and rows["hr"]["policy_denials"] == 0
    assert out["unattributed"]["turns"] == 1 and out["unattributed"]["policy_denials"] == 0
    # by_mode keeps LOG_ONLY "would deny" apart from ENFORCE denials (final review M3).
    assert {k: v for k, v in out["gateway_decisions"].items() if k != "daily"} == {
        "allow": 14, "deny": 9, "by_tool": {"t___lookup_salary": 3},
        "by_mode": {"LOG_ONLY": {"allow": 4, "deny": 7}, "ENFORCE": {"allow": 10, "deny": 2}},
    }
    assert out["sources"]["policy_metrics"] is True
    assert out["recent_denials"][0]["team"] == "finance"


def test_rows_compare_each_day_against_the_alert(monkeypatch):
    wire()
    usage = insights._usage_override
    usage.record_turn(agent_record_id="rec-1", owner_sub="s1", input_tokens=10, output_tokens=5, tool_calls={}, date="2026-10-08", turn_id="t3", team="finance")
    usage.record_turn(agent_record_id="rec-1", owner_sub="s1", input_tokens=10, output_tokens=5, tool_calls={}, date="2026-10-09", turn_id="t4", team="finance")
    # Priced flushes: 10-08 and 10-10 cross a $2 alert, 10-09 does not; the window sum ($7) alone would say nothing about days.
    usage.repository.calls.append(("TEAMS#2026-10", "D#2026-10-08#G#finance", {"model_cost_micros": 3_000_000}))
    usage.repository.calls.append(("TEAMS#2026-10", "D#2026-10-09#G#finance", {"model_cost_micros": 1_000_000}))
    usage.repository.calls.append(("TEAMS#2026-10", "D#2026-10-10#G#finance", {"model_cost_micros": 2_500_000}))
    insights._teams_override.put("finance", TeamConfigUpdate(daily_cost_alert_usd=2.0))
    monkeypatch.setattr(insights, "_require_configured", lambda: None)
    monkeypatch.setattr(insights, "_window", lambda days: {"days": days, "start_date": "2026-10-01", "end_date": "2026-10-10", "timezone": "UTC", "partial_day": "2026-10-10"})
    monkeypatch.setattr(insights.clock, "today", lambda: "2026-10-10")
    rows = {r["team"]: r for r in insights.team_insights(days=10, user=AuthUser(username="a", groups=["admin"]))["teams"]}
    fin = rows["finance"]
    assert fin["daily_cost_alert_usd"] == 2.0
    assert fin["today_cost_micros"] == 2_500_000
    assert fin["max_day_cost_micros"] == 3_000_000
    assert fin["days_over_alert"] == 2
    # No alert set: nothing to count against, so the count is None rather than 0 ("0 days over" would claim a check that never ran).
    assert rows["hr"]["days_over_alert"] is None and rows["hr"]["today_cost_micros"] == 0


class Directory:
    def __init__(self, sizes=None, broken=False):
        self.sizes, self.broken = sizes or {}, broken

    def group_members(self, team):
        if self.broken:
            raise RuntimeError("cognito down")
        return [object()] * self.sizes.get(team, 0)


def test_series_are_dense_and_the_remainder_is_per_day(monkeypatch):
    wire()
    usage = insights._usage_override
    usage.record_turn(agent_record_id="rec-1", owner_sub="s1", input_tokens=1, output_tokens=1, tool_calls={}, date="2026-10-08", turn_id="t5", team="finance")
    usage.record_policy_denial(agent_record_id="rec-1", owner_sub="s2", team=None, tool_name="t___x", date="2026-10-09")
    insights._org_directory_override = Directory({"finance": 3})
    monkeypatch.setattr(insights, "_require_configured", lambda: None)
    monkeypatch.setattr(insights, "_window", lambda days: {"days": days, "start_date": "2026-10-08", "end_date": "2026-10-10", "timezone": "UTC", "partial_day": False})
    out = insights.team_insights(days=3, user=AuthUser(username="a", groups=["admin"]))
    rows = {r["team"]: r for r in out["teams"]}
    fin = rows["finance"]
    assert [d["date"] for d in fin["daily"]] == ["2026-10-08", "2026-10-09", "2026-10-10"]
    assert [d["turns"] for d in fin["daily"]] == [1, 0, 1]
    assert [d["policy_denials"] for d in fin["daily"]] == [0, 0, 1]
    assert fin["members"] == 3 and rows["hr"]["members"] == 0
    # s2's turn on 10-10 has no team: it is the remainder that day, and only that day.
    assert [d["turns"] for d in out["unattributed"]["daily"]] == [0, 0, 1]
    assert [d["policy_denials"] for d in out["unattributed"]["daily"]] == [0, 1, 0]
    # Collector days only where measured, by mode.
    assert out["gateway_decisions"]["daily"] == [
        {"date": "2026-10-09", "by_mode": {"LOG_ONLY": {"allow": 4, "deny": 6}}},
        {"date": "2026-10-10", "by_mode": {"ENFORCE": {"allow": 10, "deny": 2}, "LOG_ONLY": {"allow": 0, "deny": 1}}},
    ]


def test_member_count_is_none_when_the_directory_fails(monkeypatch):
    wire()
    insights._org_directory_override = Directory(broken=True)
    monkeypatch.setattr(insights, "_require_configured", lambda: None)
    out = insights.team_insights(days=7, user=AuthUser(username="a", groups=["admin"]))
    assert all(r["members"] is None for r in out["teams"])
