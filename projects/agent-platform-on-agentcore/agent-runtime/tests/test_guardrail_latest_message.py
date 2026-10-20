"""The guardrail screens the newest user message on input, not the whole thread.

Measured on Converse against the bst guardrail (2026-10-18): a benign question
after an earlier turn the prompt-attack filter flags came back
`guardrail_intervened` with the whole history screened, `end_turn` with only the
newest message. In an agent loop the history also holds every tool result.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base import build_bedrock_model  # noqa: E402
from config import Config  # noqa: E402


def _config(**kw):
    base = dict(model_id="m", region_name="us-west-2", guardrail_id="g", guardrail_version="DRAFT",
                max_tokens=0, prompt_cache=False, reasoning_budget=0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_only_the_newest_message_is_screened_by_default():
    model = build_bedrock_model(_config(guardrail_latest_message=True), None, None)
    assert model.config["guardrail_latest_message"] is True


def test_whole_thread_screening_can_be_restored():
    model = build_bedrock_model(_config(guardrail_latest_message=False), None, None)
    assert model.config["guardrail_latest_message"] is False


def test_the_env_default_is_on(monkeypatch):
    monkeypatch.delenv("GUARDRAIL_LATEST_MESSAGE", raising=False)
    assert Config.from_env().guardrail_latest_message is True
    monkeypatch.setenv("GUARDRAIL_LATEST_MESSAGE", "off")
    assert Config.from_env().guardrail_latest_message is False
