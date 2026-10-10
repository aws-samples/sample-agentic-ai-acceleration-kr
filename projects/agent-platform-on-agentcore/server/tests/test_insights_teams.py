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
from services.usage_service import UsageService  # noqa: E402
from test_usage_team import StubRepo  # noqa: E402


class Prefs:
    def __init__(self):
        self.items = {}

    def get(self, sub, name):
        return self.items.get((sub, name))

    def put(self, sub, name, value):
        self.items[(sub, name)] = value


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


def wire():
    repo = StubRepo()
    usage = UsageService(repository=repo)
    usage.record_turn(agent_record_id="rec-1", owner_sub="s1", input_tokens=10, output_tokens=5, tool_calls={}, date="2026-10-10", turn_id="t1", team="finance")
    usage.record_turn(agent_record_id="rec-1", owner_sub="s2", input_tokens=10, output_tokens=5, tool_calls={}, date="2026-10-10", turn_id="t2")
    usage.record_policy_denial(agent_record_id="rec-1", owner_sub="s1", team="finance", tool_name="t___lookup_salary", date="2026-10-10")
    teams = TeamService(Prefs(), {"finance": "arn:f", "hr": "arn:h"})
    teams.put("finance", TeamConfigUpdate(label="재무팀"))
    insights._usage_override = usage
    insights._teams_override = teams
    insights._collector_override = Collector()


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
    assert out["gateway_decisions"] == {
        "allow": 14, "deny": 9, "by_tool": {"t___lookup_salary": 3},
        "by_mode": {"LOG_ONLY": {"allow": 4, "deny": 7}, "ENFORCE": {"allow": 10, "deny": 2}},
    }
    assert out["sources"]["policy_metrics"] is True
    assert out["recent_denials"][0]["team"] == "finance"
