"""The server, not the browser, decides which runtime or harness a turn invokes.

Before services/agent_access.py the stream request's `agent_runtime_arn` /
`harness_arn` were invoked as sent: any signed-in user could aim a thread at any
runtime the server's role can reach, with only the record's APPROVED status in
the way. These tests pin the binding rules and the two escape hatches that keep
existing deployments working (registry outage → status quo; no record and no
default runtime → status quo with a warning).
"""
import os
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.config as cfg  # noqa: E402
from services import agent_access  # noqa: E402
from services.agent_access import (  # noqa: E402
    AgentTargetMismatch,
    bind_execution,
    is_record_verdict,
)
from services.registry_service import RegistryNotConfigured  # noqa: E402

RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"
HARNESS = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
FOREIGN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/someone-elses"
DEFAULT = SimpleNamespace(agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/default", qualifier="DEFAULT")
NO_DEFAULT = SimpleNamespace(agent_runtime_arn=None, qualifier=None)
SONNET = "global.anthropic.claude-sonnet-5"


class Registry:
    def __init__(self, record=None, exc=None):
        self.record, self.exc, self.calls = record, exc, []

    def get_record(self, record_id):
        self.calls.append(record_id)
        if self.exc:
            raise self.exc
        return self.record
    def chattable_record(self, record_id):
        # The approval gate reads the chattable revision; these doubles have one.
        return self.get_record(record_id)


def _record(runtime=None, harness=None, qualifier=None):
    return SimpleNamespace(agent_runtime_arn=runtime, harness_arn=harness, qualifier=qualifier)


def _thread(record_id="", target=None, model=None, metadata=None):
    return SimpleNamespace(agent_record_id=record_id, agent_target=target, basic_chat_model_id=model, metadata=metadata)


def _bind(config, thread=None, registry=None, client=DEFAULT):
    return bind_execution(config, existing_thread=thread, registry_service=registry or Registry(), default_client=client)


# --- a registry record owns its target ----------------------------------------


def test_the_record_supplies_the_arns_and_they_are_remembered():
    reg = Registry(_record(runtime=RUNTIME, qualifier="v1"))
    config, learned = _bind({"registry_record_id": "rec-a"}, registry=reg)
    assert (config["agent_runtime_arn"], config["harness_arn"], config["qualifier"]) == (RUNTIME, None, "v1")
    assert learned == {"agent_runtime_arn": RUNTIME, "harness_arn": None, "qualifier": "v1"}


def test_a_client_arn_that_is_not_the_records_is_refused():
    reg = Registry(_record(runtime=RUNTIME))
    with pytest.raises(AgentTargetMismatch):
        _bind({"registry_record_id": "rec-a", "agent_runtime_arn": FOREIGN}, registry=reg)
    with pytest.raises(AgentTargetMismatch):
        _bind({"registry_record_id": "rec-a", "harness_arn": HARNESS}, registry=reg)


def test_matching_client_arns_pass_and_a_harness_record_binds_its_harness():
    reg = Registry(_record(runtime=RUNTIME, harness=HARNESS))
    config, _ = _bind({"registry_record_id": "rec-h", "harness_arn": HARNESS, "agent_runtime_arn": RUNTIME}, registry=reg)
    assert config["harness_arn"] == HARNESS


def test_a_deployed_fallback_id_is_resolved_from_the_deployed_resources(monkeypatch):
    import routes.registry as registry_routes

    monkeypatch.setattr(registry_routes, "_deployed_detail", lambda rid: _record(harness=HARNESS))
    reg = Registry(exc=AssertionError("registry must not be asked"))
    config, learned = _bind({"registry_record_id": "deployed:" + HARNESS}, registry=reg)
    assert config["harness_arn"] == HARNESS and learned["harness_arn"] == HARNESS


def test_a_record_without_a_target_refuses_a_foreign_arn():
    with pytest.raises(AgentTargetMismatch):
        _bind({"registry_record_id": "rec-empty", "agent_runtime_arn": FOREIGN}, registry=Registry(_record()))
    config, learned = _bind({"registry_record_id": "rec-empty"}, registry=Registry(_record()))
    assert config["agent_runtime_arn"] == DEFAULT.agent_runtime_arn
    assert learned["agent_runtime_arn"] == DEFAULT.agent_runtime_arn


# --- the remembered target saves the registry round-trip ---------------------


def test_a_pinned_thread_reuses_its_remembered_target_without_a_lookup():
    stored = {"agent_runtime_arn": RUNTIME, "harness_arn": None, "qualifier": None}
    reg = Registry(exc=AssertionError("no lookup expected"))
    config, learned = _bind({"agent_runtime_arn": RUNTIME}, thread=_thread("rec-a", stored), registry=reg)
    assert config["registry_record_id"] == "rec-a"
    assert config["agent_runtime_arn"] == RUNTIME
    assert learned is None and reg.calls == []


def test_a_changed_client_arn_goes_back_to_the_registry_and_a_recomposed_harness_is_accepted():
    stored = {"agent_runtime_arn": None, "harness_arn": HARNESS, "qualifier": None}
    new_harness = HARNESS + "-v2"
    reg = Registry(_record(harness=new_harness))
    config, learned = _bind({"registry_record_id": "rec-h", "harness_arn": new_harness}, thread=_thread("rec-h", stored), registry=reg)
    assert reg.calls == ["rec-h"]
    assert config["harness_arn"] == new_harness and learned["harness_arn"] == new_harness


def test_a_changed_client_arn_the_record_does_not_own_is_refused():
    stored = {"agent_runtime_arn": RUNTIME, "harness_arn": None, "qualifier": None}
    reg = Registry(_record(runtime=RUNTIME))
    with pytest.raises(AgentTargetMismatch):
        _bind({"agent_runtime_arn": FOREIGN}, thread=_thread("rec-a", stored), registry=reg)


def test_the_pinned_record_is_inherited_when_the_request_names_none():
    reg = Registry(_record(runtime=RUNTIME))
    config, _ = _bind({}, thread=_thread("rec-a"), registry=reg)
    assert config["registry_record_id"] == "rec-a" and reg.calls == ["rec-a"]


# --- no record ----------------------------------------------------------------


def test_without_a_record_only_the_default_runtime_may_be_invoked():
    config, learned = _bind({})
    assert config["agent_runtime_arn"] == DEFAULT.agent_runtime_arn and config["qualifier"] == "DEFAULT"
    assert learned is None
    with pytest.raises(AgentTargetMismatch):
        _bind({"agent_runtime_arn": FOREIGN})
    with pytest.raises(AgentTargetMismatch):
        _bind({"harness_arn": HARNESS})


def test_without_a_record_or_a_default_runtime_the_request_passes_through():
    config, learned = _bind({"agent_runtime_arn": FOREIGN}, client=NO_DEFAULT)
    assert config["agent_runtime_arn"] == FOREIGN and learned is None


# --- registry failures --------------------------------------------------------


def test_an_unconfigured_or_unreachable_registry_keeps_the_request_as_sent():
    for exc in (
        RegistryNotConfigured("off"),
        ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetRegistryRecord"),
    ):
        config, learned = _bind({"registry_record_id": "rec-a", "agent_runtime_arn": RUNTIME}, registry=Registry(exc=exc))
        assert config["agent_runtime_arn"] == RUNTIME and learned is None


@pytest.mark.parametrize("code", ["ValidationException", "ResourceNotFoundException"])
def test_a_verdict_about_the_record_id_fails_closed(code):
    exc = ClientError({"Error": {"Code": code}}, "GetRegistryRecord")
    assert is_record_verdict(exc)
    with pytest.raises(AgentTargetMismatch):
        _bind({"registry_record_id": "made-up", "agent_runtime_arn": FOREIGN}, registry=Registry(exc=exc))


def test_other_lookup_failures_fail_closed():
    with pytest.raises(AgentTargetMismatch):
        _bind({"registry_record_id": "rec-a"}, registry=Registry(exc=RuntimeError("boom")))


# --- the global model allow-list -----------------------------------------------


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(cfg, "ALLOWED_MODELS", [SONNET])


def test_a_model_override_inside_the_allow_list_passes_for_any_record(allowed):
    config, _ = _bind({"registry_record_id": "rec-a", "model_id": SONNET}, registry=Registry(_record(runtime=RUNTIME)))
    assert config["model_id"] == SONNET
    config, _ = _bind({"model_id": SONNET})  # no record: the default runtime
    assert config["model_id"] == SONNET and config["agent_runtime_arn"] == DEFAULT.agent_runtime_arn


def test_a_model_override_outside_the_allow_list_is_refused_even_for_an_admin(allowed):
    admin = SimpleNamespace(is_admin=True, teams=[])
    with pytest.raises(AgentTargetMismatch):
        _bind({"registry_record_id": "rec-a", "model_id": "global.anthropic.claude-opus-5-5"}, registry=Registry(_record(runtime=RUNTIME)))
    with pytest.raises(AgentTargetMismatch):
        bind_execution({"model_id": "global.anthropic.claude-opus-5-5"}, registry_service=Registry(), default_client=DEFAULT, caller=admin)


def test_an_empty_allow_list_refuses_every_model_override_but_not_a_plain_turn(monkeypatch):
    monkeypatch.setattr(cfg, "ALLOWED_MODELS", [])
    with pytest.raises(AgentTargetMismatch):
        _bind({"model_id": SONNET})
    config, _ = _bind({"registry_record_id": "rec-a"}, registry=Registry(_record(runtime=RUNTIME)))
    assert "model_id" not in config or not config["model_id"]


# --- threads the retired basic-chat path left behind ---------------------------

LEGACY = "__basic_chat__"
DEFAULT_RECORD = SimpleNamespace(record_id="rec-default", name="bap_default", agent_runtime_arn=DEFAULT.agent_runtime_arn, harness_arn=None, qualifier="DEFAULT")


@pytest.fixture
def default_record(monkeypatch, allowed):
    monkeypatch.setattr(agent_access, "default_record", lambda registry_service: DEFAULT_RECORD)


def test_a_legacy_thread_is_moved_onto_the_default_record_with_its_model(default_record):
    config, learned = _bind({}, thread=_thread(LEGACY, model=SONNET))
    assert config["registry_record_id"] == "rec-default"
    assert config["registry_agent_name"] == "bap_default"
    assert config["model_id"] == SONNET
    assert config["adopted_model_id"] == SONNET
    assert config["agent_runtime_arn"] == DEFAULT.agent_runtime_arn
    assert learned["agent_runtime_arn"] == DEFAULT.agent_runtime_arn


def test_a_legacy_pin_outside_the_allow_list_is_dropped_not_refused(default_record):
    config, _ = _bind({}, thread=_thread(LEGACY, model="global.anthropic.claude-haiku-4-5-20251001-v1:0"))
    assert config["registry_record_id"] == "rec-default"
    assert not config.get("model_id") and config["adopted_model_id"] is None


def test_a_model_the_client_sends_wins_over_the_legacy_pin(default_record, monkeypatch):
    monkeypatch.setattr(cfg, "ALLOWED_MODELS", [SONNET, "global.anthropic.claude-opus-5-5"])
    config, _ = _bind({"model_id": "global.anthropic.claude-opus-5-5"}, thread=_thread(LEGACY, model=SONNET))
    assert config["model_id"] == "global.anthropic.claude-opus-5-5" == config["adopted_model_id"]


def test_a_legacy_thread_accepts_the_default_record_id_and_refuses_another(default_record):
    config, _ = _bind({"registry_record_id": "rec-default"}, thread=_thread(LEGACY, model=SONNET))
    assert config["registry_record_id"] == "rec-default"
    with pytest.raises(AgentTargetMismatch):
        _bind({"registry_record_id": "rec-other"}, thread=_thread(LEGACY, model=SONNET))


def test_an_old_client_basic_chat_request_binds_the_default_record(default_record):
    # A tab loaded before the deploy still sends the retired fields and no record id.
    config, _ = _bind({"basic_chat": True, "basic_chat_model_id": SONNET, "registry_agent_name": "기본 채팅"})
    assert config["registry_record_id"] == "rec-default"
    assert config["registry_agent_name"] == "bap_default"
    assert config["model_id"] == SONNET
    assert "basic_chat" not in config and "basic_chat_model_id" not in config


def test_without_a_default_record_a_legacy_thread_is_refused(monkeypatch, allowed):
    monkeypatch.setattr(agent_access, "default_record", lambda registry_service: None)
    with pytest.raises(AgentTargetMismatch):
        _bind({}, thread=_thread(LEGACY, model=SONNET))


def test_legacy_translation_uses_the_deployed_fallback_when_the_registry_is_off(monkeypatch, allowed):
    fallback = SimpleNamespace(record_id=f"deployed:{DEFAULT.agent_runtime_arn}", name="bap_default",
                               agent_runtime_arn=DEFAULT.agent_runtime_arn, harness_arn=None, qualifier="DEFAULT")
    monkeypatch.setattr(agent_access, "default_record", lambda registry_service: fallback)
    config, learned = _bind({}, thread=_thread(LEGACY, model=SONNET), registry=Registry(exc=RegistryNotConfigured("off")))
    assert config["registry_record_id"] == fallback.record_id and learned["agent_runtime_arn"] == DEFAULT.agent_runtime_arn


def test_a_translated_turn_is_not_a_legacy_turn_afterwards(default_record):
    # Once ThreadService re-pins the thread, the next turn is an ordinary pinned turn.
    config, _ = _bind({}, thread=_thread("rec-default", target={"agent_runtime_arn": DEFAULT.agent_runtime_arn, "harness_arn": None, "qualifier": "DEFAULT"}))
    assert "adopted_model_id" not in config


def test_a_resent_legacy_pin_outside_the_allow_list_is_dropped_like_the_pin(default_record):
    # The new web seeds the thread's pinned model into its override and sends it
    # as model_id on every turn. That is the pin coming back, not a choice: it
    # must get the pin's rule (drop when no longer allowed), not a 403.
    old = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    config, _ = _bind({"model_id": old}, thread=_thread(LEGACY, model=old))
    assert config["registry_record_id"] == "rec-default"
    assert not config.get("model_id") and config["adopted_model_id"] is None
