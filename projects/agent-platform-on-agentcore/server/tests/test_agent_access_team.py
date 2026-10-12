"""A turn may only bind to a record its caller is allowed to see."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.config as cfg  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from services.agent_access import AgentTargetMismatch, bind_execution  # noqa: E402


@pytest.fixture(autouse=True)
def global_models(monkeypatch):
    # The operator's allow-list is a ceiling for every override; team lists only
    # narrow it. These tests are about the team layer, so open the ceiling.
    monkeypatch.setattr(cfg, "ALLOWED_MODELS", ["global.sonnet", "global.opus"])

RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"
DEFAULT = SimpleNamespace(agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/default", qualifier="DEFAULT")


class Registry:
    def __init__(self, team):
        self.team = team
        self.lookups = 0

    def chattable_record(self, record_id):
        meta = {"team": self.team} if self.team else None
        return SimpleNamespace(agent_runtime_arn=RUNTIME, harness_arn=None, qualifier=None,
                               custom_metadata=meta, team=self.team)

    def team_of_record(self, record_id):
        self.lookups += 1
        return self.team


def member(*teams):
    return AuthUser(username="m", groups=[f"team:{t}" for t in teams])


class Teams:
    """A TeamService stand-in: `enabled` plus the model limit lookup."""

    def __init__(self, enabled=True, models=None):
        self.enabled = enabled
        self.models = models or {}

    def allowed_models_for(self, names):
        lists = [self.models.get(n) for n in names]
        if not lists or any(not m for m in lists):
            return None
        return {m for ms in lists for m in ms}


ON = Teams()


def bind(registry, caller, existing=None, config=None, teams=ON):
    return bind_execution(config or {"registry_record_id": "rec-1"}, existing_thread=existing,
                          registry_service=registry, default_client=DEFAULT, caller=caller,
                          team_service=teams)


def test_member_of_the_team_binds_and_learns_the_team():
    config, target = bind(Registry("finance"), member("finance"))
    assert target["agent_runtime_arn"] == RUNTIME and config["record_team"] == "finance"


def test_outsider_is_refused_and_admin_is_not():
    with pytest.raises(AgentTargetMismatch):
        bind(Registry("finance"), member("hr"))
    config, _ = bind(Registry("finance"), AuthUser(username="a", groups=["admin"]))
    assert config["record_team"] == "finance"


def test_shared_record_and_no_caller_keep_working():
    config, _ = bind(Registry(None), member("hr"))
    assert config["record_team"] is None
    config, _ = bind(Registry("finance"), None)
    assert config["record_team"] == "finance"


def test_pinned_thread_is_rechecked_for_team_users_only():
    stored = {"agent_runtime_arn": RUNTIME, "harness_arn": None, "qualifier": None}
    existing = SimpleNamespace(agent_record_id="rec-1", agent_target=stored)
    registry = Registry("finance")
    with pytest.raises(AgentTargetMismatch):
        bind(registry, member("hr"), existing=existing)
    assert registry.lookups == 1
    config, learned = bind(registry, AuthUser(username="a", groups=["admin"]), existing=existing)
    assert learned is None and registry.lookups == 1   # admins skip the lookup


def test_no_declared_teams_means_no_team_checks_and_no_lookup():
    registry = Registry("search")   # a free-string label from before teams existed
    config, _ = bind(registry, member(), teams=Teams(enabled=False))
    assert config["record_team"] == "search"
    stored = {"agent_runtime_arn": RUNTIME, "harness_arn": None, "qualifier": None}
    existing = SimpleNamespace(agent_record_id="rec-1", agent_target=stored)
    bind(registry, member(), existing=existing, teams=Teams(enabled=False))
    assert registry.lookups == 0


HARNESS = "arn:aws:bedrock-agentcore:us-east-1:1:harness/fin_bot-abc"
DEPLOYED_ID = f"deployed:{HARNESS}"


@pytest.fixture
def deployed(monkeypatch):
    """`_deployed_detail` answers from the fallback listing; swap its records."""
    import routes.registry as registry_routes
    from models.registry import RegistryRecordSummary

    state = {"team": "finance", "known": True}

    class Sync:
        def deployed_agent_records(self):
            return [RegistryRecordSummary(
                record_id=DEPLOYED_ID, name="fin_bot", harness_arn=HARNESS,
                agent_runtime_arn=RUNTIME, source="deployed",
                custom_metadata={"team": state["team"]} if state["team"] else None,
                visibility_known=state["known"],
            )]

    monkeypatch.setattr(registry_routes, "_sync", lambda: Sync())
    return state


class NoRegistry:
    """A `deployed:` id must never reach GetRegistryRecord."""

    def chattable_record(self, record_id):
        raise AssertionError("deployed ids are not registry records")

    team_of_record = chattable_record


def test_deployed_harness_of_another_team_is_refused(deployed):
    # Final review C1: the fallback record used to carry no team, so any user
    # could run a team harness (and its role's tools) by its deployed: id.
    with pytest.raises(AgentTargetMismatch):
        bind(NoRegistry(), member("hr"), config={"registry_record_id": DEPLOYED_ID})
    config, target = bind(NoRegistry(), member("finance"), config={"registry_record_id": DEPLOYED_ID})
    assert target["harness_arn"] == HARNESS and config["record_team"] == "finance"


def test_deployed_harness_with_unreadable_tags_is_refused_to_non_admins(deployed):
    deployed.update(team=None, known=False)
    with pytest.raises(AgentTargetMismatch):
        bind(NoRegistry(), member("finance"), config={"registry_record_id": DEPLOYED_ID})
    config, _ = bind(NoRegistry(), AuthUser(username="a", groups=["admin"]),
                     config={"registry_record_id": DEPLOYED_ID})
    assert config["harness_arn"] == HARNESS


def test_pinned_deployed_id_is_checked_through_the_tag_not_get_record(deployed):
    # Final review I2: GetRegistryRecord("deployed:…") is a ValidationException,
    # which used to 403 every turn of such a thread once the registry was back.
    stored = {"agent_runtime_arn": RUNTIME, "harness_arn": HARNESS, "qualifier": None}
    existing = SimpleNamespace(agent_record_id=DEPLOYED_ID, agent_target=stored)
    config, learned = bind(NoRegistry(), member("finance"), existing=existing,
                           config={"registry_record_id": DEPLOYED_ID})
    assert learned is None and config["record_team"] == "finance"
    with pytest.raises(AgentTargetMismatch):
        bind(NoRegistry(), member("hr"), existing=existing, config={"registry_record_id": DEPLOYED_ID})


class Unavailable:
    def _boom(self, record_id):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "ServiceUnavailableException", "Message": "down"}}, "GetRegistryRecord")

    chattable_record = team_of_record = _boom


def test_registry_outage_fails_closed_for_non_admins_with_teams():
    with pytest.raises(AgentTargetMismatch):
        bind(Unavailable(), member("finance"))
    config, learned = bind(Unavailable(), AuthUser(username="a", groups=["admin"]))
    assert learned is None
    config, learned = bind(Unavailable(), member(), teams=Teams(enabled=False))
    assert learned is None


def test_model_override_is_held_to_the_record_teams_list():
    # Final review I3: a per-thread override used to bypass the team's model list.
    teams = Teams(models={"finance": ["global.sonnet"]})
    cfg_ok = {"registry_record_id": "rec-1", "model_id": "global.sonnet"}
    config, _ = bind(Registry("finance"), member("finance"), config=cfg_ok, teams=teams)
    assert config["model_id"] == "global.sonnet"
    with pytest.raises(AgentTargetMismatch):
        bind(Registry("finance"), member("finance"), teams=teams,
             config={"registry_record_id": "rec-1", "model_id": "global.opus"})
    # Admins and records of an unrestricted team are not limited.
    bind(Registry("finance"), AuthUser(username="a", groups=["admin"]), teams=teams,
         config={"registry_record_id": "rec-1", "model_id": "global.opus"})
    bind(Registry(None), member("hr"), teams=teams,
         config={"registry_record_id": "rec-1", "model_id": "global.opus"})


def test_model_override_on_a_pinned_thread_is_checked_every_turn():
    teams = Teams(models={"finance": ["global.sonnet"]})
    stored = {"agent_runtime_arn": RUNTIME, "harness_arn": None, "qualifier": None}
    existing = SimpleNamespace(agent_record_id="rec-1", agent_target=stored)
    with pytest.raises(AgentTargetMismatch):
        bind(Registry("finance"), member("finance"), existing=existing, teams=teams,
             config={"registry_record_id": "rec-1", "model_id": "global.opus"})
