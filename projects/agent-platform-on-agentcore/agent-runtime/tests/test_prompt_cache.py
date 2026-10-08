"""프롬프트 캐싱이 실제로 모델에 붙는지 — 그리고 붙으면 안 되는 곳엔 안 붙는지.

`cacheReadInputTokens`가 채워지려면 Converse 요청에 cachePoint가 있어야 하고,
Strands는 `cache_prompt`/`cache_tools`에서 그 마커를 만든다. 그런데 cachePoint는
캐싱을 지원하지 않는 모델에서는 no-op이 아니라 *검증 오류*라, Anthropic 모델에만
붙여야 한다. 이 두 성질이 깨지면 인사이트의 캐시 읽기가 조용히 0으로 돌아가거나(마커
누락) 다른 모델을 고른 순간 턴이 통째로 실패한다 — 둘 다 배포에서만 드러난다.
"""
import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from agents.base import build_bedrock_model  # noqa: E402


def _cache_fields(config, model_id=None):
    # cache_prompt는 이 Strands 버전에서 deprecation 경고를 내지만 동작은 한다.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = build_bedrock_model(config, model_id, "us-east-1")
    cfg = model.get_config()
    return cfg.get("cache_prompt"), cfg.get("cache_tools")


def test_caching_on_for_anthropic_by_default():
    prompt, tools = _cache_fields(Config())
    assert prompt == "default"
    assert tools == "default"


def test_no_cachepoint_on_unsupported_model():
    # 캐싱을 지원하지 않는 모델에 cachePoint를 붙이면 Converse가 요청을 거절한다.
    prompt, tools = _cache_fields(Config(), "amazon.titan-text-premier-v1:0")
    assert prompt is None
    assert tools is None


def test_flag_off_disables_caching():
    prompt, tools = _cache_fields(Config(prompt_cache=False))
    assert prompt is None
    assert tools is None


def test_env_turns_caching_off(monkeypatch):
    monkeypatch.setenv("PROMPT_CACHE", "false")
    assert Config.from_env().prompt_cache is False
    monkeypatch.setenv("PROMPT_CACHE", "true")
    assert Config.from_env().prompt_cache is True


# Config() defaults to Sonnet 5, which takes adaptive thinking; the budgeted-form
# tests pin a Claude 4.x id so they keep exercising the budget arithmetic.
LEGACY_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def _reasoning_fields(config, model_id=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = build_bedrock_model(config, model_id, "us-east-1").get_config()
    return cfg.get("additional_request_fields"), cfg.get("temperature")


def test_reasoning_off_by_default_builds_model_unchanged():
    """Budget 0 must not touch the request: no thinking field, no forced temp."""
    fields, temp = _reasoning_fields(Config())
    assert fields is None
    assert temp is None


def test_reasoning_budget_enables_thinking_and_forces_temperature():
    fields, temp = _reasoning_fields(Config(reasoning_budget=2048), LEGACY_MODEL)
    assert fields == {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    assert temp == 1.0


def test_claude_5_models_get_adaptive_thinking_instead_of_a_budget():
    """Sonnet 5 rejects thinking.type=enabled (ValidationException at ConverseStream);
    the same budget on a Claude 5 id must become the adaptive block."""
    for model_id in ("global.anthropic.claude-sonnet-5", "global.anthropic.claude-opus-5-5",
                     "us.anthropic.claude-haiku-5-20270101-v1:0"):
        fields, _ = _reasoning_fields(Config(reasoning_budget=2048), model_id)
        assert fields == {"thinking": {"type": "adaptive"}}, model_id
    # Earlier generations keep the budgeted form.
    fields, _ = _reasoning_fields(Config(reasoning_budget=2048), "global.anthropic.claude-opus-4-8")
    assert fields == {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    fields, _ = _reasoning_fields(Config(reasoning_budget=2048), "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert fields["thinking"]["type"] == "enabled"


def test_reasoning_budget_is_floored_to_the_anthropic_minimum():
    fields, _ = _reasoning_fields(Config(reasoning_budget=100), LEGACY_MODEL)
    assert fields["thinking"]["budget_tokens"] == 1024


def test_reasoning_budget_cannot_meet_or_exceed_max_tokens():
    """budget_tokens must stay below max_tokens; the builder makes room."""
    fields, _ = _reasoning_fields(Config(reasoning_budget=8192, max_tokens=8192), LEGACY_MODEL)
    assert fields["thinking"]["budget_tokens"] < 8192


def test_env_sets_reasoning_budget(monkeypatch):
    monkeypatch.setenv("REASONING_BUDGET", "3000")
    assert Config.from_env().reasoning_budget == 3000
    monkeypatch.delenv("REASONING_BUDGET", raising=False)
    assert Config.from_env().reasoning_budget == 0
