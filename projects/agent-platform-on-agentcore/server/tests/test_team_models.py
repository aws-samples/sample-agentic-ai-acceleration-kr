"""A team's model list narrows the global allow-list; it never widens it."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.config as cfg  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from models.harness import ComposeHarnessRequest  # noqa: E402
from models.team import TeamConfigUpdate  # noqa: E402
from services.agent_access import AgentTargetMismatch, bind_execution  # noqa: E402
from services.harness_service import HarnessService  # noqa: E402
from services.team_service import TeamService  # noqa: E402

SONNET, OPUS, HAIKU = "global.sonnet", "global.opus", "global.haiku"
DEFAULT = SimpleNamespace(agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/default", qualifier="DEFAULT")
ROLES = {"finance": "arn:aws:iam::1:role/f", "hr": "arn:aws:iam::1:role/h"}


class Prefs:
    def __init__(self):
        self.items = {}

    def get(self, sub, name):
        return self.items.get((sub, name))

    def put(self, sub, name, value):
        self.items[(sub, name)] = value


@pytest.fixture
def teams():
    t = TeamService(Prefs(), ROLES)
    t.put("finance", TeamConfigUpdate(allowed_models=[SONNET]))
    return t


@pytest.fixture(autouse=True)
def global_models(monkeypatch):
    monkeypatch.setattr(cfg, "ALLOWED_MODELS", [SONNET, OPUS])


def member(*names):
    return AuthUser(username="m", groups=[f"team:{n}" for n in names])


def override(model, caller, teams):
    """A model override on the default runtime (no record), the shape every
    agent's override takes now that basic chat is a record like the others."""
    return bind_execution({"model_id": model}, registry_service=None,
                          default_client=DEFAULT, caller=caller, team_service=teams)


def test_team_member_is_limited_to_the_team_list(teams):
    assert override(SONNET, member("finance"), teams)[0]["model_id"] == SONNET
    with pytest.raises(AgentTargetMismatch):
        override(OPUS, member("finance"), teams)


def test_the_global_list_is_a_ceiling_for_everyone_and_team_lists_only_narrow_it(teams):
    with pytest.raises(AgentTargetMismatch):
        override(HAIKU, member("finance"), teams)      # not globally allowed
    with pytest.raises(AgentTargetMismatch):
        override(HAIKU, AuthUser(username="a", groups=["admin"]), teams)   # admins too
    assert override(OPUS, AuthUser(username="a", groups=["admin"]), teams)[0]["model_id"] == OPUS
    assert override(OPUS, member(), teams)[0]["model_id"] == OPUS
    assert override(OPUS, member("hr"), teams)[0]["model_id"] == OPUS   # hr has no list → no limit


def test_harness_model_outside_the_team_list_is_refused(teams):
    class Control:
        def create_harness(self, **params):
            self.params = params
            return {"harness": {"harnessId": "h", "arn": "arn:h", "harnessName": params["harnessName"], "status": "CREATING"}}
    svc = HarnessService(registry=object(), region="us-east-1", execution_role_arn="arn:aws:iam::1:role/x")
    svc._control = Control()
    svc._resolve_mcp_tools = lambda ids: []
    svc._resolve_skills = lambda record_ids, paths: []
    svc.teams = teams
    with pytest.raises(ValueError):
        svc.create_harness(ComposeHarnessRequest(name="fin", team="finance", model_id=OPUS))
    svc.create_harness(ComposeHarnessRequest(name="fin", team="finance", model_id=SONNET))
    assert svc._control.params["model"]["bedrockModelConfig"]["modelId"] == SONNET


def test_a_legacy_pin_outside_the_team_list_is_dropped_not_refused(teams, monkeypatch):
    # A retired basic-chat thread pinned to a model the caller's team no longer
    # allows must still move onto the default agent (with the agent's default
    # model); refusing would leave the thread stuck on `__basic_chat__` forever.
    from services import agent_access
    record = SimpleNamespace(record_id="rec-default", name="bap_default",
                             agent_runtime_arn=DEFAULT.agent_runtime_arn, harness_arn=None, qualifier="DEFAULT")
    monkeypatch.setattr(agent_access, "default_record", lambda registry_service: record)
    thread = SimpleNamespace(agent_record_id="__basic_chat__", agent_target=None, basic_chat_model_id=OPUS, metadata=None)
    config, _ = bind_execution({}, existing_thread=thread, registry_service=None, default_client=DEFAULT,
                               caller=member("finance"), team_service=teams)
    assert config["registry_record_id"] == "rec-default"
    assert not config.get("model_id") and config["adopted_model_id"] is None
    # An explicit choice of that same model is still refused like any override.
    with pytest.raises(AgentTargetMismatch):
        bind_execution({"model_id": OPUS}, existing_thread=SimpleNamespace(agent_record_id="__basic_chat__", agent_target=None, basic_chat_model_id=SONNET, metadata=None),
                       registry_service=None, default_client=DEFAULT, caller=member("finance"), team_service=teams)
