"""An agent's own model / system prompt, shown as the baseline in the override popover."""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.registry as reg  # noqa: E402
from models.registry import RegistryRecordDetail  # noqa: E402
from services.agent_defaults import resolve_agent_defaults  # noqa: E402


class _Usage:
    def __init__(self, model_id):
        self.model_id = model_id
        self.calls = []

    def resolve_model_for(self, *, harness_arn=None, agent_runtime_arn=None):
        self.calls.append((harness_arn, agent_runtime_arn))
        return self.model_id


class _Harness:
    def __init__(self, system_prompt=None, fail=False):
        self.system_prompt = system_prompt
        self.fail = fail
        self.asked = []

    def get_harness(self, harness_id):
        self.asked.append(harness_id)
        if self.fail:
            raise RuntimeError("GetHarness failed")

        class H:
            pass

        h = H()
        h.system_prompt = self.system_prompt
        h.model_id = "ignored-here"
        return h


HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/harness_x-AbC123"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_default-AbCdE12345"


def _record(**kw):
    return RegistryRecordDetail(record_id="r1", name="x", status="APPROVED", **kw)


def test_harness_agent_reports_both_model_and_prompt():
    usage = _Usage("global.anthropic.claude-sonnet-5-5")
    harness = _Harness(system_prompt="You are a careful analyst.")
    out = resolve_agent_defaults(_record(harness_arn=HARNESS_ARN), usage=usage, harness=harness)
    assert out.model_id == "global.anthropic.claude-sonnet-5-5"
    assert out.system_prompt == "You are a careful analyst."
    # The harness id, not the ARN, is what GetHarness takes.
    assert harness.asked == ["harness_x-AbC123"]
    assert usage.calls == [(HARNESS_ARN, None)]


def test_runtime_agent_reports_model_but_no_prompt():
    # A runtime's prompt is a constant inside its container; nothing can read it.
    usage = _Usage("global.anthropic.claude-haiku-5-5")
    harness = _Harness(system_prompt="never used")
    out = resolve_agent_defaults(
        _record(agent_runtime_arn=RUNTIME_ARN), usage=usage, harness=harness
    )
    assert out.model_id == "global.anthropic.claude-haiku-5-5"
    assert out.system_prompt is None
    assert harness.asked == []
    assert usage.calls == [(None, RUNTIME_ARN)]


def test_non_agent_record_reports_nothing_without_calling_aws():
    usage = _Usage("would-be-wrong")
    out = resolve_agent_defaults(_record(descriptor_type="MCP"), usage=usage, harness=_Harness())
    assert out.model_id is None and out.system_prompt is None
    assert usage.calls == []


def test_failed_harness_read_still_reports_the_model():
    usage = _Usage("global.anthropic.claude-sonnet-5-5")
    out = resolve_agent_defaults(
        _record(harness_arn=HARNESS_ARN), usage=usage, harness=_Harness(fail=True)
    )
    assert out.model_id == "global.anthropic.claude-sonnet-5-5"
    assert out.system_prompt is None


def test_route_answers_from_the_record_and_hides_invisible_ones(monkeypatch):
    detail = _record(harness_arn=HARNESS_ARN)
    monkeypatch.setattr(reg._service, "get_record", lambda record_id: detail)
    monkeypatch.setattr(reg, "_usage_service", _Usage("global.anthropic.claude-opus-5-5"))
    monkeypatch.setattr(reg, "_harness_service", _Harness(system_prompt="Be brief."))
    out = reg.get_record_defaults(record_id="r1", user=None)
    assert out.model_id == "global.anthropic.claude-opus-5-5"
    assert out.system_prompt == "Be brief."

    monkeypatch.setattr(reg, "_can_see_detail", lambda detail, user: False)
    with pytest.raises(HTTPException) as exc:
        reg.get_record_defaults(record_id="r1", user=None)
    assert exc.value.status_code == 404
