# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Normalize the `thinking` field per target model for Bedrock/Mantle path.

Anthropic changed the extended-thinking API between model generations, and the
two shapes are mutually exclusive across models:

    model                       thinking:{type:"enabled"}   thinking:{type:"adaptive"}
    anthropic.claude-opus-4-8   400 (not supported)         200
    anthropic.claude-opus-4-7   400 (not supported)         200
    anthropic.claude-haiku-4-5  200                         400 (not supported)

Opus 4.7+ dropped the fixed-budget form (`enabled` + `budget_tokens`) in favour
of `adaptive` + `output_config.effort`; Haiku 4.5 predates adaptive thinking and
only accepts the old form. The gateway normalizes here since it's the only layer
that knows the target model.

Unknown models pass through untouched: a newly-added model behaves exactly as it
does today rather than inheriting a guessed rule. The shim never raises — worst
case is the provider's own error, never one we introduced.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Models that require `thinking:{type:"adaptive"}` and reject the legacy form.
_ADAPTIVE_ONLY_PREFIXES: tuple[str, ...] = (
    "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-4-8",
    "anthropic.claude-opus-5",
    "anthropic.claude-sonnet-5",
    "anthropic.claude-fable-5",
    "anthropic.claude-mythos-5",
    # Haiku 5.5 — measured on Bedrock us-west-2 (2026-10-10): `enabled` is a 400
    # ("thinking.type.enabled is not supported for this model. Use thinking.type.adaptive
    # and output_config.effort"), `adaptive` is 200. Haiku 4.5 is the opposite (legacy
    # list below), so "haiku" alone never decides the family — the version does.
    "anthropic.claude-haiku-5-5",
)

# Models that accept only the legacy `enabled` form and reject `adaptive`.
_LEGACY_ONLY_PREFIXES: tuple[str, ...] = (
    "anthropic.claude-haiku-4-5",
)

_DEFAULT_BUDGET_TOKENS = 4096
_MIN_BUDGET_TOKENS = 1024


def _family(provider_model_id: str | None) -> str | None:
    """Return "adaptive", "legacy", or None (unknown — leave request alone)."""
    if not provider_model_id:
        return None
    mid = provider_model_id.lower()
    # Drop whatever routes the call so the vendor-prefixed name is matched: a
    # cross-region prefix ("us.", "apac.", "global.", "jp.", "au.", "in.", "ca." —
    # measured 2026-10-13 across 17 commercial regions) or a system inference-profile
    # ARN. Found by the vendor segment, not a prefix list: a list misses the next zone
    # (a Korea-only profile would likely be "kr."), and an id that matches no family
    # skips normalisation — a 400 whenever the thinking form has to change (fallback
    # or downgrade across generations, or a client sending the old form).
    i = mid.find("anthropic.")
    if i > 0:
        mid = mid[i:]
    if mid.startswith(_ADAPTIVE_ONLY_PREFIXES):
        return "adaptive"
    if mid.startswith(_LEGACY_ONLY_PREFIXES):
        return "legacy"
    return None


def _family_from_alias(alias: str | None) -> str | None:
    """alias 문자열로 계열을 추정한다. ``provider_model_id`` 를 모르는 계층용.

    ⚠️ 왜 필요한가: 예산 강등 미들웨어는 모델 **해석 전**에 동작하므로 alias 만 안다.
       그 계층이 ``startswith("claude-haiku-4-5")`` 로 판정하고 있었는데, 운영자가 만든
       alias(예: ``team-haiku-cheap``)는 그 접두사로 시작하지 않아 판정을 빠져나갔다 —
       강등 대상이 haiku 인데 thinking 이 그대로 실려 400 이 됐다.

    ⚠️ 접두사가 아니라 **부분 문자열**로 본다. alias 는 운영자가 자유롭게 짓는 이름이라
       접두사 규약을 강제할 수 없다. 대가는 오탐 가능성이지만, 두 변환 모두 상류가
       거부하는 형태를 받아들이는 형태로 바꾸는 것이라 오탐의 비용이 낮다.

    ⚠️ "haiku" 만으로는 계열이 정해지지 않는다(2026-10-10). haiku-4-5 는 legacy,
       haiku-5-5 는 adaptive 전용이다. 예전처럼 haiku 면 무조건 legacy 로 보면, 강등
       경로가 Haiku 5.5 의 adaptive 를 enabled 로 바꾸고 output_config 를 지워 400 이 난다.
       그래서 haiku 뒤의 버전을 읽는다: 5 이상 → adaptive, 4.x → legacy. 버전을 읽을 수
       없으면(``cowork-haiku``, 날짜형 ``claude-haiku-20251001``) 예전과 같은 legacy.
    """
    if not alias:
        return None
    a = alias.lower()
    if "haiku" in a:
        return _haiku_family(a)
    for token in ("opus-4-7", "opus-4-8", "opus-5", "sonnet-5", "fable-5", "mythos-5"):
        if token in a:
            return "adaptive"
    return None


def _haiku_family(alias: str) -> str:
    """Family of a haiku alias by its version: ``4.5``/``4-5``/``4_5``/``45`` → legacy,
    5 and up → adaptive; no readable version → legacy (the behaviour before the split).
    """
    match = re.search(r"haiku\D{0,2}(\d+)(?:[.\-_](\d+))?", alias)
    if match is None:
        return "legacy"
    major_raw, minor = match.group(1), match.group(2)
    if minor is not None or len(major_raw) == 1:
        major = int(major_raw)
    elif len(major_raw) == 2:
        major = int(major_raw[0])          # "haiku45" is 4.5, not version 45
    else:
        return "legacy"                    # a date stamp or build number, not a version
    return "adaptive" if major >= 5 else "legacy"


def normalize_thinking(
    body: dict[str, Any],
    provider_model_id: str | None,
    *,
    alias: str | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Return `body` with `thinking` adjusted to what `provider_model_id` accepts.

    Mutates and returns the same dict (callers build a throwaway body dict).
    Never raises: any unexpected shape is passed through untouched.
    """
    try:
        thinking = body.get("thinking")
        if not isinstance(thinking, dict):
            return body

        t_type = thinking.get("type")
        if t_type not in ("enabled", "adaptive"):
            return body

        # alias 추정은 provider id 가 **없을 때만**(강등 미들웨어). id 가 있는데 목록에 없는
        # 새 모델은 "그대로 통과"가 계약이다 — 추정이 legacy 쪽으로 기울어 있어서, 새 모델의
        # adaptive 를 enabled 로 바꿔 400 을 만들 수 있다.
        # English: guess from the alias only when there is no provider id (the downgrade
        # middleware). An unrecognised provider id means "leave the body alone".
        family = _family(provider_model_id)
        if family is None and provider_model_id is None:
            family = _family_from_alias(alias)
        if family is None:
            return body

        # ⚠️ legacy 계열은 `output_config` 자체를 받지 않는다. `thinking` 이 이미
        #    `enabled` 라 아래 변환이 필요 없는 경우에도 이건 떨궈야 한다 — 그러지 않으면
        #    `output_config` 를 허용 필드에 넣은 순간 haiku 가 400 을 내기 시작한다.
        if family == "legacy" and "output_config" in body:
            body.pop("output_config", None)
            logger.info(
                "thinking_normalized",
                request_id=request_id,
                provider_model_id=provider_model_id,
                direction="output_config_dropped_legacy_family",
            )

        if family == "adaptive" and t_type == "enabled":
            new_thinking: dict[str, Any] = {"type": "adaptive"}
            if "display" in thinking:
                new_thinking["display"] = thinking["display"]
            body["thinking"] = new_thinking
            logger.info(
                "thinking_normalized",
                request_id=request_id,
                provider_model_id=provider_model_id,
                direction="enabled_to_adaptive",
                dropped_budget_tokens=thinking.get("budget_tokens"),
            )
            return body

        if family == "legacy" and t_type == "adaptive":
            max_tokens = body.get("max_tokens")
            budget = _DEFAULT_BUDGET_TOKENS
            if isinstance(max_tokens, int):
                budget = min(budget, max_tokens - 1)
            if budget < _MIN_BUDGET_TOKENS:
                body.pop("thinking", None)
                body.pop("output_config", None)
                logger.info(
                    "thinking_normalized",
                    request_id=request_id,
                    provider_model_id=provider_model_id,
                    direction="adaptive_dropped_no_budget_room",
                    max_tokens=max_tokens,
                )
                return body
            new_thinking = {"type": "enabled", "budget_tokens": budget}
            if "display" in thinking:
                new_thinking["display"] = thinking["display"]
            body["thinking"] = new_thinking
            had_output_config = "output_config" in body
            body.pop("output_config", None)
            logger.info(
                "thinking_normalized",
                request_id=request_id,
                provider_model_id=provider_model_id,
                direction="adaptive_to_enabled",
                budget_tokens=budget,
                dropped_output_config=had_output_config,
            )
            return body

        return body
    except Exception:
        logger.warning(
            "thinking_normalize_failed",
            request_id=request_id,
            provider_model_id=provider_model_id,
            exc_info=True,
        )
        return body
