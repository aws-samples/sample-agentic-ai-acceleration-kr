# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
