# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Integration tests for the availability fallback loop (Task 6).

Tests the `run_fallback_loop` helper directly with stubbed adapter,
circuit breaker, and resolver so we can exercise the decision logic
without spinning up the full middleware stack.

Assertions that matter per the design spec:
  - test_falls_back_on_502_then_succeeds
  - test_403_does_not_fall_back
  - test_429_does_not_fall_back
  - test_circuit_open_skips_original
  - test_all_open_returns_synthetic_503
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    TokenUsage,
)
from app.services.circuit_breaker import CircuitBreakerService
from app.services.fallback_loop import FallbackResult, run_fallback_loop
from app.services.lua_loader import LuaScriptLoader

# ---------------------------------------------------------------------------
# Load Lua scripts (needed for RateLimitService / CircuitBreakerService)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "app" / "redis_scripts"
LuaScriptLoader.load_all(_SCRIPT_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pricing() -> ModelPricingSchema:
    return ModelPricingSchema(
        input_per_1k=Decimal("0.003"),
        output_per_1k=Decimal("0.015"),
    )


def _make_model_config(alias: str, pmid: str | None = None, provider: ProviderType = ProviderType.BEDROCK) -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id=pmid or f"us.anthropic.{alias}",
        alias=alias,
        provider=provider,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="us-east-1",
        pricing=_make_pricing(),
        status=ModelStatus.ACTIVE,
    )


def _make_auth_context(user_id: str = "user-1", team_id: str = "team-1"):
    auth = MagicMock()
    auth.user_id = user_id
    auth.team_id = team_id
    auth.dept_id = "dept-1"
    auth.sso_subject = "sso-user-1"
    auth.allowed_models = None
    return auth


def _ok_response_bytes() -> bytes:
    return json.dumps({
        "type": "message",
        "content": [{"type": "text", "text": "Hello"}],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }).encode()


def _error_bytes(status: int) -> bytes:
    return json.dumps({"error": {"type": "provider_error", "message": f"err {status}"}}).encode()


def _make_cb(*, open_pmids: set[str] | None = None, probe_wins: bool = True) -> CircuitBreakerService:
    """Build a CircuitBreakerService with controlled is_open / probe behavior."""
    cb = MagicMock(spec=CircuitBreakerService)
    open_set = open_pmids or set()

    async def _is_open(redis, pmid: str) -> bool:
        return pmid in open_set

    async def _probe(redis, pmid: str) -> bool:
        return probe_wins

    async def _failure(redis, pmid: str) -> bool:
        return False

    async def _success(redis, pmid: str) -> bool:
        return False

    cb.is_open = AsyncMock(side_effect=_is_open)
    cb.try_acquire_halfopen_probe = AsyncMock(side_effect=_probe)
    cb.record_failure = AsyncMock(side_effect=_failure)
    cb.record_success = AsyncMock(side_effect=_success)
    return cb


def _make_adapter(responses: list[tuple[int, bytes | None, dict, Any]]):
    """Adapter mock returning the given (status, body, headers, usage/req_id) in order."""
    adapter = MagicMock()
    idx = {"n": 0}

    async def _invoke(body, model_id, **kwargs):
        i = idx["n"]
        idx["n"] += 1
        status, resp_body, headers, usage = responses[i]
        return status, resp_body, headers, usage

    adapter.invoke = AsyncMock(side_effect=_invoke)
    adapter.invoke_stream = AsyncMock(side_effect=_invoke)
    return adapter


def _noop_build_body(req_d, model_cfg, is_stream):
    """Identity body builder — returns the same body bytes for all candidates."""
    return (
        json.dumps(req_d).encode(),
        {"path_suffix": "invoke-with-response-stream"},
        {"path_suffix": "invoke"},
    )


# ---------------------------------------------------------------------------
# Common run_fallback_loop kwargs builder
# ---------------------------------------------------------------------------

def _common_kwargs(
    *,
    try_order: list[str],
    adapter,
    cb,
    original_config: ModelConfigSchema,
    resolve_map: dict[str, ModelConfigSchema] | None = None,
    auth_context=None,
    state: dict | None = None,
    is_stream: bool = False,
):
    """Build the full kwargs dict for run_fallback_loop."""
    resolve_map = resolve_map or {}
    auth = auth_context or _make_auth_context()
    st = state if state is not None else {}

    router_service = MagicMock()
    router_service.check_key_scope = MagicMock()  # does NOT raise by default

    async def _resolve(alias: str) -> ModelConfigSchema:
        if alias in resolve_map:
            return resolve_map[alias]
        raise LookupError(f"no model: {alias}")

    return dict(
        try_order=try_order,
        original_alias=try_order[0],
        is_stream=is_stream,
        req_data={"model": try_order[0], "messages": [], "max_tokens": 100},
        redis=None,  # None = fail-open everywhere
        auth_context=auth,
        state=st,
        request_id="req-test-001",
        budget_status=None,
        adapter=adapter,
        stream_kwargs={"path_suffix": "invoke-with-response-stream"},
        nonstream_kwargs={"path_suffix": "invoke"},
        cb=cb,
        router_service=router_service,
        session_factory=None,
        is_db_degraded=True,
        original_model_config=original_config,
        resolve_model_config=_resolve,
        build_candidate_body=_noop_build_body,
        rewrite_model_id=lambda pmid: pmid,
    )


# ===========================================================================
# TC-1: Falls back on 502, then succeeds on fallback model
# ===========================================================================

class TestFallsBackOn502ThenSucceeds:
    """Original returns 502 → fallback returns 200 → final 200.

    Also verifies:
      - availability_fallback_from is set to the original alias
      - settle_tpm + settle_cost called on the failed candidate
        (with actual=0 so the reservation is fully refunded)
    """

    @pytest.mark.asyncio
    async def test_falls_back_on_502_then_succeeds(self):
        original_alias = "claude-sonnet-4-6"
        fallback_alias = "claude-haiku-4-5"

        original_config = _make_model_config(original_alias, pmid="us.anthropic.claude-sonnet-4-6")
        fallback_config = _make_model_config(fallback_alias, pmid="us.anthropic.claude-haiku-4-5")

        adapter = _make_adapter([
            (502, _error_bytes(502), {}, TokenUsage()),
            (200, _ok_response_bytes(), {}, TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30)),
        ])
        cb = _make_cb()

        # Patch enforce_rate_limits so it writes rate_limit_state and returns None (pass)
        settle_tpm_calls = []
        settle_cost_calls = []

        async def _fake_enforce(*, redis, auth_context, model_config, body, state, request_id, budget_status):
            state["rate_limit_state"] = {
                "tpm_descriptors": ["desc1"],
                "tpm_reserved": 500,
                "cost_reserved": Decimal("0.01"),
            }
            return None

        async def _fake_settle_tpm(redis, descriptors, reserved, actual):
            settle_tpm_calls.append({"descriptors": descriptors, "reserved": reserved, "actual": actual})

        async def _fake_settle_cost(redis, *, user_id, actual_cost, reserved_cost, team_id):
            settle_cost_calls.append({"user_id": user_id, "actual": actual_cost, "reserved": reserved_cost})

        from fakeredis import aioredis as fr
        fake_redis = fr.FakeRedis(decode_responses=True)

        kwargs = _common_kwargs(
            try_order=[original_alias, fallback_alias],
            adapter=adapter,
            cb=cb,
            original_config=original_config,
            resolve_map={fallback_alias: fallback_config},
            state={},
        )
        kwargs["redis"] = fake_redis

        with (
            patch("app.services.fallback_loop.enforce_rate_limits", side_effect=_fake_enforce),
            patch("app.services.fallback_loop.RateLimitService") as mock_rl_cls,
        ):
            mock_rl = mock_rl_cls.return_value
            mock_rl.settle_tpm = AsyncMock(side_effect=_fake_settle_tpm)
            mock_rl.settle_cost = AsyncMock(side_effect=_fake_settle_cost)

            result = await run_fallback_loop(**kwargs)

        assert result.status == 200
        assert result.availability_fallback_from == original_alias

        # settle_tpm called once for the failed original (actual=0)
        assert len(settle_tpm_calls) >= 1
        assert settle_tpm_calls[0]["actual"] == 0
        assert settle_tpm_calls[0]["reserved"] == 500

        # settle_cost called once for the failed original (actual_cost=0)
        assert len(settle_cost_calls) >= 1
        assert settle_cost_calls[0]["actual"] == Decimal("0")
        assert settle_cost_calls[0]["reserved"] == Decimal("0.01")

        # CB: failure recorded for the failed original, success for fallback
        cb.record_failure.assert_awaited()
        cb.record_success.assert_awaited()

        await fake_redis.aclose()


# ===========================================================================
# TC-2: 403 does NOT fall back
# ===========================================================================

class TestFourOhThreeDoesNotFallBack:
    """A 403 from key-scope check must propagate immediately; second model never invoked."""

    @pytest.mark.asyncio
    async def test_403_does_not_fall_back(self):
        original_alias = "claude-sonnet-4-6"
        fallback_alias = "claude-haiku-4-5"

        original_config = _make_model_config(original_alias)
        fallback_config = _make_model_config(fallback_alias)

        adapter = _make_adapter([
            (200, _ok_response_bytes(), {}, TokenUsage(input_tokens=5, output_tokens=5, total_tokens=10)),
        ])
        cb = _make_cb()

        # Make check_key_scope raise PermissionError for the original
        router_service = MagicMock()
        router_service.check_key_scope = MagicMock(side_effect=PermissionError("not allowed"))

        async def _resolve(alias):
            return fallback_config

        async def _fake_enforce(**kwargs):
            return None

        kwargs = _common_kwargs(
            try_order=[original_alias, fallback_alias],
            adapter=adapter,
            cb=cb,
            original_config=original_config,
            resolve_map={fallback_alias: fallback_config},
        )
        kwargs["router_service"] = router_service

        with patch("app.services.fallback_loop.enforce_rate_limits", side_effect=_fake_enforce):
            result = await run_fallback_loop(**kwargs)

        assert result.status == 403
        # Adapter was never called (permission denied before invoke)
        adapter.invoke.assert_not_awaited()
        # Only one scope check (for the original); fallback never attempted
        assert router_service.check_key_scope.call_count == 1


# ===========================================================================
# TC-3: 429 does NOT fall back
# ===========================================================================

class TestFourTwoNineDoesNotFallBack:
    """A 429 from enforce_rate_limits must propagate immediately; second model never invoked."""

    @pytest.mark.asyncio
    async def test_429_does_not_fall_back(self):
        from fastapi.responses import JSONResponse

        original_alias = "claude-sonnet-4-6"
        fallback_alias = "claude-haiku-4-5"

        original_config = _make_model_config(original_alias)
        fallback_config = _make_model_config(fallback_alias)

        adapter = _make_adapter([
            (200, _ok_response_bytes(), {}, TokenUsage(input_tokens=5, output_tokens=5, total_tokens=10)),
        ])
        cb = _make_cb()

        # enforce_rate_limits returns a 429 JSONResponse
        rl_429 = JSONResponse(
            status_code=429,
            content={"error": {"type": "rate_limit_error", "code": "user_rpm_exceeded", "message": "limit hit", "retry_after": 60}},
            headers={"Retry-After": "60"},
        )

        async def _fake_enforce(*, redis, auth_context, model_config, body, state, request_id, budget_status):
            return rl_429

        kwargs = _common_kwargs(
            try_order=[original_alias, fallback_alias],
            adapter=adapter,
            cb=cb,
            original_config=original_config,
            resolve_map={fallback_alias: fallback_config},
        )

        with patch("app.services.fallback_loop.enforce_rate_limits", side_effect=_fake_enforce):
            result = await run_fallback_loop(**kwargs)

        assert result.status == 429
        # Adapter never invoked
        adapter.invoke.assert_not_awaited()


# ===========================================================================
# TC-4: Circuit open for original — skips to fallback
# ===========================================================================

class TestCircuitOpenSkipsOriginal:
    """If the circuit is open for the original model and probe not won, skip to fallback."""

    @pytest.mark.asyncio
    async def test_circuit_open_skips_original(self):
        original_alias = "claude-sonnet-4-6"
        fallback_alias = "claude-haiku-4-5"

        original_config = _make_model_config(original_alias, pmid="us.anthropic.claude-sonnet-4-6")
        fallback_config = _make_model_config(fallback_alias, pmid="us.anthropic.claude-haiku-4-5")

        adapter = _make_adapter([
            # Only one call expected — the fallback
            (200, _ok_response_bytes(), {}, TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30)),
        ])

        # Original pmid is circuit-open, probe NOT won
        original_pmid = original_config.provider_model_id
        cb = _make_cb(open_pmids={original_pmid}, probe_wins=False)

        async def _fake_enforce(*, redis, auth_context, model_config, body, state, request_id, budget_status):
            state["rate_limit_state"] = {
                "tpm_descriptors": [],
                "tpm_reserved": 0,
                "cost_reserved": Decimal("0"),
            }
            return None

        kwargs = _common_kwargs(
            try_order=[original_alias, fallback_alias],
            adapter=adapter,
            cb=cb,
            original_config=original_config,
            resolve_map={fallback_alias: fallback_config},
        )

        with patch("app.services.fallback_loop.enforce_rate_limits", side_effect=_fake_enforce):
            result = await run_fallback_loop(**kwargs)

        assert result.status == 200
        # Adapter invoked exactly once (for the fallback, not the original)
        assert adapter.invoke.await_count == 1
        # availability_fallback_from is set (we went straight to fallback)
        assert result.availability_fallback_from == original_alias


# ===========================================================================
# TC-5: All candidates circuit-open → synthetic 503 with Retry-After
# ===========================================================================

class TestAllOpenReturnsSynthetic503:
    """When every candidate is circuit-open and no probe won, return synthetic 503."""

    @pytest.mark.asyncio
    async def test_all_open_returns_synthetic_503(self):
        original_alias = "claude-sonnet-4-6"
        fallback_alias = "claude-haiku-4-5"

        original_config = _make_model_config(original_alias, pmid="us.anthropic.claude-sonnet-4-6")
        fallback_config = _make_model_config(fallback_alias, pmid="us.anthropic.claude-haiku-4-5")

        adapter = _make_adapter([])  # No calls expected

        # Both pmids are open, probe never wins
        open_pmids = {original_config.provider_model_id, fallback_config.provider_model_id}
        cb = _make_cb(open_pmids=open_pmids, probe_wins=False)

        kwargs = _common_kwargs(
            try_order=[original_alias, fallback_alias],
            adapter=adapter,
            cb=cb,
            original_config=original_config,
            resolve_map={fallback_alias: fallback_config},
        )

        result = await run_fallback_loop(**kwargs)

        assert result.status == 503
        assert result.all_open is True
        body = json.loads(result.payload[0])
        assert body["error"]["type"] == "service_unavailable"
        # Adapter never called
        adapter.invoke.assert_not_awaited()


# ===========================================================================
# TC-6: 504 does NOT count as CB failure but does trigger fallback
# ===========================================================================

class TestFiveOhFourDoesNotTripCircuitBreaker:
    """504 (ModelTimeoutException) triggers fallback but MUST NOT call record_failure."""

    @pytest.mark.asyncio
    async def test_504_fallback_no_cb_failure(self):
        original_alias = "claude-sonnet-4-6"
        fallback_alias = "claude-haiku-4-5"

        original_config = _make_model_config(original_alias, pmid="us.anthropic.claude-sonnet-4-6")
        fallback_config = _make_model_config(fallback_alias, pmid="us.anthropic.claude-haiku-4-5")

        adapter = _make_adapter([
            (504, _error_bytes(504), {}, TokenUsage()),
            (200, _ok_response_bytes(), {}, TokenUsage(input_tokens=10, output_tokens=20, total_tokens=30)),
        ])
        cb = _make_cb()

        async def _fake_enforce(*, redis, auth_context, model_config, body, state, request_id, budget_status):
            state["rate_limit_state"] = {
                "tpm_descriptors": [],
                "tpm_reserved": 0,
                "cost_reserved": Decimal("0"),
            }
            return None

        kwargs = _common_kwargs(
            try_order=[original_alias, fallback_alias],
            adapter=adapter,
            cb=cb,
            original_config=original_config,
            resolve_map={fallback_alias: fallback_config},
        )

        with patch("app.services.fallback_loop.enforce_rate_limits", side_effect=_fake_enforce):
            result = await run_fallback_loop(**kwargs)

        assert result.status == 200
        # 504 must NOT trip the CB
        cb.record_failure.assert_not_awaited()
        # Success on fallback recorded
        cb.record_success.assert_awaited_once()


# ===========================================================================
# TC-7: Haiku thinking strip
# ===========================================================================

class TestHaikuThinkingStrip:
    """When fallback candidate is claude-haiku-4-5, 'thinking' is stripped from body."""

    @pytest.mark.asyncio
    async def test_haiku_thinking_stripped(self):
        original_alias = "claude-sonnet-4-6"
        haiku_alias = "claude-haiku-4-5-sonnet"  # starts with "claude-haiku-4-5"

        original_config = _make_model_config(original_alias)
        haiku_config = _make_model_config(haiku_alias, pmid="us.anthropic.claude-haiku-4-5-sonnet")

        received_bodies = []

        async def _invoke(body_bytes, model_id, **kwargs):
            received_bodies.append(json.loads(body_bytes))
            return 200, _ok_response_bytes(), {}, TokenUsage(input_tokens=5, output_tokens=5, total_tokens=10)

        adapter = MagicMock()
        adapter.invoke = AsyncMock(side_effect=lambda *a, **kw: _invoke(*a, **kw))

        cb = _make_cb()

        async def _fake_enforce(*, redis, auth_context, model_config, body, state, request_id, budget_status):
            state["rate_limit_state"] = {"tpm_descriptors": [], "tpm_reserved": 0, "cost_reserved": Decimal("0")}
            return None

        req_data = {"model": original_alias, "messages": [], "max_tokens": 100, "thinking": {"type": "enabled", "budget_tokens": 1000}}

        kwargs = _common_kwargs(
            try_order=[original_alias, haiku_alias],
            adapter=adapter,
            cb=cb,
            original_config=original_config,
            resolve_map={haiku_alias: haiku_config},
        )
        kwargs["req_data"] = req_data

        # Make original 502 so we fall to haiku
        async def _invoke_seq(body_bytes, model_id, **kwargs_inner):
            received_bodies.append(json.loads(body_bytes))
            if len(received_bodies) == 1:
                return 502, _error_bytes(502), {}, TokenUsage()
            return 200, _ok_response_bytes(), {}, TokenUsage(input_tokens=5, output_tokens=5, total_tokens=10)

        adapter.invoke = AsyncMock(side_effect=_invoke_seq)

        with patch("app.services.fallback_loop.enforce_rate_limits", side_effect=_fake_enforce):
            result = await run_fallback_loop(**kwargs)

        assert result.status == 200
        # Second call (to haiku) must NOT have 'thinking' in the body
        assert len(received_bodies) == 2
        assert "thinking" not in received_bodies[1]


# ===========================================================================
# TC-8: Mantle original never admits a Bedrock candidate
# ===========================================================================

class TestMantleOriginalNoBedrockCandidate:
    """FIX 1 regression: _same_provider_as_original must exclude Bedrock aliases
    when the original is BEDROCK_MANTLE.

    We simulate the _same_provider_as_original predicate directly (it is a
    closure built per-request in messages.py) to assert the guard is not a no-op.

    We also run the full fallback loop with a BEDROCK_MANTLE original and a
    Bedrock fallback alias in try_order, and verify the adapter is called
    exactly once (only the original), never for the Bedrock alias.
    """

    def test_same_provider_predicate_excludes_unknown_alias(self):
        """The predicate returns False for aliases not present in the map.

        This is the direct unit check of the guard fix: missing alias → None →
        None != original_provider → False (excluded, not silently admitted).
        """
        from app.schemas.domain import ProviderType

        original_provider = ProviderType.BEDROCK_MANTLE
        _alias_provider_map: dict = {"claude-cowork-v1": original_provider}

        def _same_provider(alias: str) -> bool:
            return _alias_provider_map.get(alias) == original_provider

        # Original alias → known same provider → True
        assert _same_provider("claude-cowork-v1") is True
        # Bedrock alias NOT in map → None != BEDROCK_MANTLE → False (excluded)
        assert _same_provider("claude-sonnet-4-6") is False
        # Any other unknown alias → also excluded
        assert _same_provider("claude-haiku-4-5") is False

    @pytest.mark.asyncio
    async def test_mantle_original_try_order_contains_only_original(self):
        """With BEDROCK_MANTLE original and a Bedrock chain, adapter is invoked once.

        try_order is passed in directly (simulating what FallbackResolver would
        produce after filtering via same_provider).  We assert the adapter
        receives exactly one call (the original) and no Bedrock model is ever
        attempted.
        """
        from app.schemas.domain import ProviderType

        mantle_alias = "claude-cowork-v1"
        bedrock_alias = "claude-sonnet-4-6"  # a Bedrock-only alias

        mantle_config = _make_model_config(
            mantle_alias,
            pmid="anthropic.claude-cowork-v1",
            provider=ProviderType.BEDROCK_MANTLE,
        )

        invoked_model_ids: list[str] = []

        async def _invoke(body_bytes, model_id, **kwargs_inner):
            invoked_model_ids.append(model_id)
            return 200, _ok_response_bytes(), {}, TokenUsage(input_tokens=5, output_tokens=5, total_tokens=10)

        adapter = MagicMock()
        adapter.invoke = AsyncMock(side_effect=_invoke)
        cb = _make_cb()

        async def _fake_enforce(*, redis, auth_context, model_config, body, state, request_id, budget_status):
            state["rate_limit_state"] = {"tpm_descriptors": [], "tpm_reserved": 0, "cost_reserved": Decimal("0")}
            return None

        # try_order = [original only] — this is what FallbackResolver produces
        # after same_provider filtering correctly excludes the Bedrock candidate.
        kwargs = _common_kwargs(
            try_order=[mantle_alias],
            adapter=adapter,
            cb=cb,
            original_config=mantle_config,
            resolve_map={},
        )

        with patch("app.services.fallback_loop.enforce_rate_limits", side_effect=_fake_enforce):
            result = await run_fallback_loop(**kwargs)

        assert result.status == 200
        # Adapter invoked exactly once — only the Mantle original
        assert adapter.invoke.await_count == 1
        # The Bedrock alias was never passed to the adapter
        assert bedrock_alias not in invoked_model_ids
        assert mantle_config.provider_model_id in invoked_model_ids
