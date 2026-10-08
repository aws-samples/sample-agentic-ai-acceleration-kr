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
    BasicChatUnavailable,
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


def _record(runtime=None, harness=None, qualifier=None):
    return SimpleNamespace(agent_runtime_arn=runtime, harness_arn=harness, qualifier=qualifier)


def _thread(record_id="", target=None, model=None):
    return SimpleNamespace(agent_record_id=record_id, agent_target=target, basic_chat_model_id=model)


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


# --- basic chat ---------------------------------------------------------------


@pytest.fixture
def basic(monkeypatch):
    monkeypatch.setattr(cfg, "BASIC_CHAT_ALLOWED_MODELS", [SONNET])
    monkeypatch.setattr(cfg, "BASIC_CHAT_RUNTIME_ARN", "")


def test_basic_chat_binds_the_default_runtime_and_the_chosen_model(basic):
    config, learned = _bind({"basic_chat": True, "basic_chat_model_id": SONNET})
    assert config["agent_runtime_arn"] == DEFAULT.agent_runtime_arn
    assert config["registry_record_id"] == cfg.BASIC_CHAT_RECORD_ID
    assert config["registry_agent_name"] == cfg.BASIC_CHAT_AGENT_NAME
    assert config["model_id"] == SONNET
    assert learned["agent_runtime_arn"] == DEFAULT.agent_runtime_arn


def test_basic_chat_refuses_a_foreign_runtime_or_a_harness(basic):
    with pytest.raises(AgentTargetMismatch):
        _bind({"basic_chat": True, "basic_chat_model_id": SONNET, "agent_runtime_arn": FOREIGN})
    with pytest.raises(AgentTargetMismatch):
        _bind({"basic_chat": True, "basic_chat_model_id": SONNET, "harness_arn": HARNESS})


def test_basic_chat_refuses_a_model_outside_the_allow_list(basic):
    with pytest.raises(BasicChatUnavailable):
        _bind({"basic_chat": True, "basic_chat_model_id": "global.anthropic.claude-opus-5-5"})
    with pytest.raises(BasicChatUnavailable):
        _bind({"basic_chat": True})


def test_basic_chat_is_refused_when_not_configured(monkeypatch):
    monkeypatch.setattr(cfg, "BASIC_CHAT_ALLOWED_MODELS", [])
    with pytest.raises(BasicChatUnavailable):
        _bind({"basic_chat": True, "basic_chat_model_id": SONNET})


def test_a_dedicated_basic_chat_runtime_wins_over_the_default(monkeypatch, basic):
    monkeypatch.setattr(cfg, "BASIC_CHAT_RUNTIME_ARN", RUNTIME)
    config, _ = _bind({"basic_chat": True, "basic_chat_model_id": SONNET})
    assert config["agent_runtime_arn"] == RUNTIME


def test_a_basic_chat_thread_continues_with_its_pinned_model_when_the_request_omits_it(basic):
    thread = _thread(cfg.BASIC_CHAT_RECORD_ID, model=SONNET)
    config, _ = _bind({}, thread=thread)
    assert config["basic_chat"] is True and config["model_id"] == SONNET


def test_basic_chat_and_a_registry_record_cannot_be_combined(basic):
    with pytest.raises(AgentTargetMismatch):
        _bind({"basic_chat": True, "basic_chat_model_id": SONNET, "registry_record_id": "rec-a"})
    with pytest.raises(AgentTargetMismatch):
        _bind({"registry_record_id": "rec-a", "basic_chat_model_id": SONNET}, registry=Registry(_record(runtime=RUNTIME)))
