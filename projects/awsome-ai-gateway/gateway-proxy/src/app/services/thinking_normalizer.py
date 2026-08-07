# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
    for geo in ("us.", "eu.", "apac.", "global."):
        if mid.startswith(geo):
            mid = mid[len(geo):]
            break
    if mid.startswith(_ADAPTIVE_ONLY_PREFIXES):
        return "adaptive"
    if mid.startswith(_LEGACY_ONLY_PREFIXES):
        return "legacy"
    return None


def normalize_thinking(
    body: dict[str, Any],
    provider_model_id: str | None,
    *,
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

        family = _family(provider_model_id)
        if family is None:
            return body

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
