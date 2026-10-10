"""A team's harness runs as the team's role, with the team's tool allow-list."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import AuthUser  # noqa: E402
from models.harness import ComposeHarnessRequest  # noqa: E402
from models.team import TeamConfigUpdate  # noqa: E402
from services.harness_service import HarnessService  # noqa: E402
from services.team_service import TeamService  # noqa: E402

ROLES = {"finance": "arn:aws:iam::1:role/bap-harness-finance", "hr": "arn:aws:iam::1:role/bap-harness-hr"}
DEFAULT_ROLE = "arn:aws:iam::1:role/bap-harness-execution"


class Prefs:
    def __init__(self):
        self.items = {}

    def get(self, sub, name):
        return self.items.get((sub, name))

    def put(self, sub, name, value):
        self.items[(sub, name)] = value


class RecordingControl:
    def __init__(self):
        self.params = None

    def create_harness(self, **params):
        self.params = params
        return {"harness": {"harnessId": "h-1", "arn": "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1",
                            "harnessName": params["harnessName"], "status": "CREATING"}}


def service(teams=None):
    control = RecordingControl()
    svc = HarnessService(registry=object(), region="us-east-1", execution_role_arn=DEFAULT_ROLE)
    svc._control = control
    svc._resolve_mcp_tools = lambda ids: []
    svc._resolve_skills = lambda record_ids, paths: []
    svc.teams = teams
    return svc, control


def teams_with_finance_tools():
    t = TeamService(Prefs(), ROLES)
    t.put("finance", TeamConfigUpdate(allowed_tools=["@builtin", "@bap-platform-tools/approve_expense"]))
    return t


def member(*names):
    return AuthUser(username="m", groups=[f"team:{n}" for n in names])


def test_team_harness_uses_team_role_tools_and_tag():
    svc, control = service(teams_with_finance_tools())
    svc.create_harness(ComposeHarnessRequest(name="fin_bot", team="finance"))
    assert control.params["executionRoleArn"] == ROLES["finance"]
    assert control.params["allowedTools"] == ["@builtin", "@bap-platform-tools/approve_expense"]
    assert control.params["tags"]["Team"] == "finance"


def test_explicit_allowed_tools_win_over_the_team_default_and_no_team_keeps_old_behaviour():
    svc, control = service(teams_with_finance_tools())
    svc.create_harness(ComposeHarnessRequest(name="fin_bot", team="finance", allowed_tools=["@builtin"]))
    assert control.params["allowedTools"] == ["@builtin"]
    svc.create_harness(ComposeHarnessRequest(name="plain"))
    assert control.params["executionRoleArn"] == DEFAULT_ROLE
    assert "allowedTools" not in control.params and "Team" not in control.params["tags"]


def test_unknown_team_is_refused():
    svc, _ = service(teams_with_finance_tools())
    with pytest.raises(ValueError):
        svc.create_harness(ComposeHarnessRequest(name="x", team="ghost"))


def test_resolve_team_picks_the_only_team_requires_a_choice_otherwise_and_checks_membership():
    svc, _ = service(TeamService(Prefs(), ROLES))
    assert svc.resolve_team(ComposeHarnessRequest(name="x"), member("hr")) == "hr"
    assert svc.resolve_team(ComposeHarnessRequest(name="x"), member()) is None
    with pytest.raises(ValueError):
        svc.resolve_team(ComposeHarnessRequest(name="x"), member("hr", "finance"))
    assert svc.resolve_team(ComposeHarnessRequest(name="x", team="finance"), member("hr", "finance")) == "finance"
    with pytest.raises(ValueError):
        svc.resolve_team(ComposeHarnessRequest(name="x", team="finance"), member("hr"))
    admin = AuthUser(username="a", groups=["admin"])
    assert svc.resolve_team(ComposeHarnessRequest(name="x", team="hr"), admin) == "hr"
    assert svc.resolve_team(ComposeHarnessRequest(name="x"), admin) is None


def test_without_teams_configured_a_team_request_is_refused_and_plain_requests_pass():
    svc, _ = service(TeamService(None, {}))
    assert svc.resolve_team(ComposeHarnessRequest(name="x"), member("hr")) is None
    with pytest.raises(ValueError):
        svc.resolve_team(ComposeHarnessRequest(name="x", team="hr"), member("hr"))


class Records:
    """registry.get_record by id; a missing id raises like a ResourceNotFound."""

    def __init__(self, teams):
        self.teams = teams

    def get_record(self, record_id):
        from types import SimpleNamespace

        team = self.teams[record_id]
        return SimpleNamespace(custom_metadata={"team": team} if team else None)


def composed(svc, req, caller):
    svc._spawn_registration = lambda *a, **kw: None
    import services.harness_service as hs
    original = hs.registry_enabled
    hs.registry_enabled = lambda: False
    try:
        return svc.compose_and_register(req, caller)
    finally:
        hs.registry_enabled = original


def test_member_cannot_widen_the_team_tool_list_but_an_admin_can():
    # Final review I4: `allowed_tools: ["*"]` from a member used to win over
    # the list the team admin set.
    svc, control = service(teams_with_finance_tools())
    composed(svc, ComposeHarnessRequest(name="fin_bot", allowed_tools=["*"]), member("finance"))
    assert control.params["allowedTools"] == ["@builtin", "@bap-platform-tools/approve_expense"]
    admin = AuthUser(username="a", groups=["admin"])
    composed(svc, ComposeHarnessRequest(name="fin_bot", team="finance", allowed_tools=["*"]), admin)
    assert control.params["allowedTools"] == ["*"]


def test_member_of_a_team_without_a_tool_list_keeps_their_own():
    svc, control = service(teams_with_finance_tools())
    composed(svc, ComposeHarnessRequest(name="hr_bot", allowed_tools=["@builtin"]), member("hr"))
    assert control.params["allowedTools"] == ["@builtin"]


def test_composing_from_another_teams_record_is_refused():
    svc, control = service(teams_with_finance_tools())
    svc.registry = Records({"m-fin": "finance", "m-shared": None, "s-hr": "hr"})
    with pytest.raises(ValueError, match="m-fin"):
        composed(svc, ComposeHarnessRequest(name="hr_bot", mcp_record_ids=["m-shared", "m-fin"]), member("hr"))
    assert control.params is None
    composed(svc, ComposeHarnessRequest(name="hr_bot", mcp_record_ids=["m-shared"], skill_record_ids=["s-hr"]),
             member("hr"))
    assert control.params is not None


def test_unreadable_record_is_refused_to_members_and_admins_skip_the_check():
    svc, control = service(teams_with_finance_tools())
    svc.registry = Records({})
    with pytest.raises(ValueError, match="ghost"):
        composed(svc, ComposeHarnessRequest(name="hr_bot", skill_record_ids=["ghost"]), member("hr"))
    composed(svc, ComposeHarnessRequest(name="x", skill_record_ids=["ghost"]), AuthUser(username="a", groups=["admin"]))
    assert control.params is not None
