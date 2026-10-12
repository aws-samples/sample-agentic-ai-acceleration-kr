# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for per-model `thinking` normalization.

Ground truth measured against Tokyo Mantle on 2026-08-06:

    model                       thinking:{enabled}  thinking:{adaptive}  omitted
    anthropic.claude-opus-4-8   400                 200                  200
    anthropic.claude-opus-4-7   400                 200                  200
    anthropic.claude-haiku-4-5  200                 400                  200
"""

import pytest

from app.services.thinking_normalizer import normalize_thinking

OPUS_48 = "anthropic.claude-opus-4-8"
OPUS_47 = "anthropic.claude-opus-4-7"
HAIKU_45 = "anthropic.claude-haiku-4-5"


def _body(**kw):
    b = {"model": "x", "max_tokens": 2048, "messages": [{"role": "user", "content": "hi"}]}
    b.update(kw)
    return b


# --- adaptive-only family (Opus 4.7 / 4.8): enabled → adaptive -----------------


@pytest.mark.parametrize("model_id", [OPUS_47, OPUS_48])
def test_enabled_converted_to_adaptive(model_id):
    b = normalize_thinking(
        _body(thinking={"type": "enabled", "budget_tokens": 1024}), model_id
    )
    assert b["thinking"] == {"type": "adaptive"}


def test_enabled_to_adaptive_drops_budget_tokens():
    b = normalize_thinking(
        _body(thinking={"type": "enabled", "budget_tokens": 32000}), OPUS_48
    )
    assert "budget_tokens" not in b["thinking"]


def test_enabled_to_adaptive_preserves_display():
    b = normalize_thinking(
        _body(thinking={"type": "enabled", "budget_tokens": 1024, "display": "summarized"}),
        OPUS_48,
    )
    assert b["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_adaptive_untouched_on_adaptive_family():
    b = normalize_thinking(_body(thinking={"type": "adaptive"}), OPUS_48)
    assert b["thinking"] == {"type": "adaptive"}


def test_output_config_kept_on_adaptive_family():
    b = normalize_thinking(
        _body(thinking={"type": "adaptive"}, output_config={"effort": "high"}), OPUS_48
    )
    assert b["output_config"] == {"effort": "high"}


# --- legacy-only family (Haiku 4.5): adaptive → enabled -----------------------


def test_adaptive_converted_to_enabled():
    b = normalize_thinking(_body(thinking={"type": "adaptive"}), HAIKU_45)
    assert b["thinking"]["type"] == "enabled"
    assert b["thinking"]["budget_tokens"] >= 1024


def test_adaptive_to_enabled_strips_output_config():
    b = normalize_thinking(
        _body(thinking={"type": "adaptive"}, output_config={"effort": "high"}), HAIKU_45
    )
    assert "output_config" not in b


def test_budget_stays_below_max_tokens():
    b = normalize_thinking(
        _body(max_tokens=2000, thinking={"type": "adaptive"}), HAIKU_45
    )
    assert b["thinking"]["budget_tokens"] < 2000


def test_thinking_dropped_when_max_tokens_too_small():
    b = normalize_thinking(_body(max_tokens=64, thinking={"type": "adaptive"}), HAIKU_45)
    assert "thinking" not in b
    assert "output_config" not in b


def test_enabled_untouched_on_legacy_family():
    b = normalize_thinking(
        _body(thinking={"type": "enabled", "budget_tokens": 1024}), HAIKU_45
    )
    assert b["thinking"] == {"type": "enabled", "budget_tokens": 1024}


# --- pass-through cases -------------------------------------------------------


def test_disabled_never_touched():
    for model_id in (OPUS_48, HAIKU_45):
        b = normalize_thinking(_body(thinking={"type": "disabled"}), model_id)
        assert b["thinking"] == {"type": "disabled"}


def test_no_thinking_field_is_noop():
    b = normalize_thinking(_body(), OPUS_48)
    assert "thinking" not in b


def test_unknown_model_left_untouched():
    original = {"type": "enabled", "budget_tokens": 1024}
    b = normalize_thinking(_body(thinking=dict(original)), "anthropic.some-future-model")
    assert b["thinking"] == original


def test_none_model_id_left_untouched():
    original = {"type": "enabled", "budget_tokens": 1024}
    b = normalize_thinking(_body(thinking=dict(original)), None)
    assert b["thinking"] == original


def test_geo_prefixed_model_id_resolves():
    b = normalize_thinking(
        _body(thinking={"type": "enabled", "budget_tokens": 1024}),
        "us.anthropic.claude-opus-4-8",
    )
    assert b["thinking"] == {"type": "adaptive"}


def test_versioned_haiku_id_resolves():
    b = normalize_thinking(
        _body(thinking={"type": "adaptive"}), "anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    assert b["thinking"]["type"] == "enabled"


def test_malformed_thinking_does_not_raise():
    for bad in ("enabled", 42, [], None):
        b = normalize_thinking(_body(thinking=bad), OPUS_48)
        assert b["thinking"] == bad


def test_other_fields_preserved():
    b = normalize_thinking(
        _body(thinking={"type": "enabled", "budget_tokens": 1024}, system="sys",
              tools=[{"name": "t", "input_schema": {}}]),
        OPUS_48,
    )
    assert b["system"] == "sys"
    assert b["tools"] == [{"name": "t", "input_schema": {}}]
    assert b["messages"] == [{"role": "user", "content": "hi"}]


# --- Haiku 5.5: adaptive-only, unlike Haiku 4.5 ----------------------------------
# Measured on Bedrock us-west-2 (us.anthropic.claude-haiku-5-5, 2026-10-10):
#   thinking:{enabled}  -> 400 "thinking.type.enabled" is not supported for this model.
#                          Use "thinking.type.adaptive" and "output_config.effort" ...
#   thinking:{adaptive} -> 200
# So "haiku" alone cannot decide the family; the version does.

from app.services.thinking_normalizer import _family_from_alias  # noqa: E402

HAIKU_55 = "anthropic.claude-haiku-5-5"


@pytest.mark.parametrize("model_id", [HAIKU_55, "us.anthropic.claude-haiku-5-5"])
def test_haiku_55_enabled_converted_to_adaptive(model_id):
    b = normalize_thinking(
        _body(thinking={"type": "enabled", "budget_tokens": 1024}), model_id
    )
    assert b["thinking"] == {"type": "adaptive"}


def test_haiku_55_keeps_adaptive_and_output_config():
    b = normalize_thinking(
        _body(thinking={"type": "adaptive"}, output_config={"effort": "low"}), HAIKU_55
    )
    assert b["thinking"] == {"type": "adaptive"}
    assert b["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("alias,family", [
    ("claude-haiku-5-5", "adaptive"),
    ("team-haiku-5.5-fast", "adaptive"),
    ("claude-haiku-4-5", "legacy"),
    ("claude-haiku-4-5-20251001", "legacy"),
    ("claudecode-haiku-4.5", "legacy"),
    ("haiku45", "legacy"),
    ("cowork-haiku", "legacy"),
    ("claude-haiku-20251001", "legacy"),
])
def test_alias_family_reads_the_haiku_version(alias, family):
    assert _family_from_alias(alias) == family


def test_downgrade_to_haiku_55_alias_keeps_adaptive():
    # The budget-downgrade layer runs before model resolution: no provider id, alias only.
    b = normalize_thinking(
        _body(thinking={"type": "adaptive"}, output_config={"effort": "low"}),
        None, alias="claude-haiku-5-5",
    )
    assert b["thinking"] == {"type": "adaptive"}
    assert b["output_config"] == {"effort": "low"}


def test_unrecognised_provider_id_is_not_guessed_from_the_alias():
    # A provider id the lists do not know means "leave the body alone" — the alias
    # guess is only for callers that have no provider id at all.
    body = _body(thinking={"type": "adaptive"}, output_config={"effort": "low"})
    b = normalize_thinking(body, "anthropic.claude-haiku-9-9", alias="team-haiku-cheap")
    assert b["thinking"] == {"type": "adaptive"}
    assert b["output_config"] == {"effort": "low"}


# --- Route prefixes: any zone, not a fixed list ---------------------------------
# Measured 2026-10-13 (`aws bedrock list-inference-profiles`, 17 commercial regions):
# us. eu. apac. jp. au. in. ca. global. — Seoul has apac./global. only today. A prefix
# list misses the next zone (a Korea-only profile would likely be `kr.`), and an id
# that matches no family skips normalisation, so the vendor segment is found instead.

from app.services.thinking_normalizer import _family  # noqa: E402

ROUTE_PREFIXES = ["", "us.", "eu.", "apac.", "global.", "jp.", "au.", "in.", "ca.",
                  "us-gov.", "kr."]


@pytest.mark.parametrize("prefix", ROUTE_PREFIXES)
def test_route_prefix_resolves_adaptive_family(prefix):
    b = normalize_thinking(
        _body(thinking={"type": "enabled", "budget_tokens": 1024}), prefix + HAIKU_55
    )
    assert b["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("prefix", ROUTE_PREFIXES)
def test_route_prefix_resolves_legacy_family(prefix):
    b = normalize_thinking(
        _body(thinking={"type": "adaptive"}),
        prefix + "anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    assert b["thinking"]["type"] == "enabled"


def test_system_inference_profile_arn_resolves():
    arn = ("arn:aws:bedrock:ap-northeast-2:123456789012:inference-profile/"
           "global.anthropic.claude-haiku-5-5")
    assert _family(arn) == "adaptive"


@pytest.mark.parametrize("model_id", [
    # an application inference profile ARN carries no model name
    "arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/a1b2c3d4e5f6",
    "us.amazon.nova-pro-v1:0",
    "openai.gpt-oss-120b-1:0",
])
def test_ids_without_a_claude_model_name_are_left_alone(model_id):
    assert _family(model_id) is None
    body = _body(thinking={"type": "enabled", "budget_tokens": 1024})
    assert normalize_thinking(body, model_id)["thinking"] == {
        "type": "enabled", "budget_tokens": 1024}
