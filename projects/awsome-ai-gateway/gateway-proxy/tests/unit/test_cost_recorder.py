# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.schemas.domain import TokenUsage
from app.services.cost_recorder import COST_STREAM_KEY, CostRecorder, calculate_cost


def test_calculate_cost_basic(model_config_bedrock):
    usage = TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500)
    cost = calculate_cost(usage, model_config_bedrock)
    # (1000/1000 * 0.003) + (500/1000 * 0.015) = 0.003 + 0.0075 = 0.0105
    assert cost == Decimal("0.010500")


def test_calculate_cost_zero_usage(model_config_bedrock):
    usage = TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0)
    cost = calculate_cost(usage, model_config_bedrock)
    assert cost == Decimal("0.000000")


@pytest.mark.asyncio
async def test_finalize_publishes_to_stream(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """FR-3.3 리팩터: DB INSERT 대신 XADD cost:stream 호출 확인."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"

    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":null}'
    )
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder()
    usage = TokenUsage(input_tokens=500, output_tokens=300, total_tokens=800)

    cost = await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=usage,
        request_id="req-001",
        is_stream=False,
        duration_ms=150,
    )

    assert cost > Decimal("0")
    # XADD 호출 확인 + payload 포함
    mock_redis.xadd.assert_awaited_once()
    args, kwargs = mock_redis.xadd.call_args
    assert args[0] == COST_STREAM_KEY
    assert "payload" in args[1]
    # MAXLEN approx trim 으로 Stream 무한 증가 방지
    assert kwargs.get("maxlen") == 100_000
    assert kwargs.get("approximate") is True


@pytest.mark.asyncio
async def test_finalize_xadd_payload_contains_cache_tokens_and_flags(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """XADD payload에 cache 토큰 + is_streaming + estimated_usage 포함 검증."""
    import json as _json

    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["budget_deduct"] = "-- mock"

    mock_redis.eval = AsyncMock(
        return_value=b'{"new_used":1.0,"remaining":99.0,"threshold_triggered":80}'
    )
    mock_redis.xadd = AsyncMock()

    recorder = CostRecorder()
    usage = TokenUsage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cache_creation_input_tokens=20,
        cache_read_input_tokens=10,
        estimated=True,  # KI-08 tokenizer 역산 흔적
    )

    await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=usage,
        request_id="req-ki08",
        is_stream=True,
        duration_ms=300,
    )

    args, _ = mock_redis.xadd.call_args
    fields = args[1]
    payload = _json.loads(fields["payload"])
    assert payload["request_id"] == "req-ki08"
    assert payload["cache_creation_tokens"] == 20
    assert payload["cache_read_tokens"] == 10
    assert payload["is_streaming"] is True
    assert payload["estimated_usage"] is True
    assert payload["threshold_triggered"] == 80


@pytest.mark.asyncio
async def test_finalize_zero_usage_only_settles_tpm_ki08(
    mock_redis, auth_context_vk, model_config_bedrock
):
    """스트리밍 disconnect + tokenizer 역산도 실패한 zero-usage 경로:
    TPM 예약만 해제, budget_deduct/XADD 모두 호출 안 됨."""
    from app.services.rate_limit_scope import RateLimitScope, ScopeDescriptor

    mock_redis.incrby = AsyncMock()
    mock_redis.eval = AsyncMock(return_value=b'{}')
    mock_redis.xadd = AsyncMock()

    descriptors = [
        ScopeDescriptor(
            scope=RateLimitScope.USER,
            scope_id="u1",
            model_alias="claude-opus",
            tpm_limit=10000,
        ),
        ScopeDescriptor(
            scope=RateLimitScope.TEAM,
            scope_id="t1",
            model_alias="claude-opus",
            tpm_limit=100000,
        ),
    ]

    recorder = CostRecorder()
    zero_usage = TokenUsage(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )

    cost = await recorder.finalize(
        redis=mock_redis,
        auth_context=auth_context_vk,
        model_config=model_config_bedrock,
        usage=zero_usage,
        request_id="req-disconnect",
        is_stream=True,
        duration_ms=500,
        rate_limit_state={
            "tpm_descriptors": descriptors,
            "tpm_reserved": 5000,
        },
    )

    assert cost == Decimal("0")
    # TPM settle만 호출 (actual=0, reserved=5000 → -5000 환불, scope 2개).
    # settle_tpm 은 이제 파이프라인으로 묶어 incrby 한다(deepdive Q50).
    pipe = mock_redis.pipeline.return_value
    assert pipe.incrby.call_count == 2
    for call in pipe.incrby.call_args_list:
        assert call.args[1] == -5000
    # budget_deduct Lua / XADD 모두 호출 안 됨
    mock_redis.eval.assert_not_called()
    mock_redis.xadd.assert_not_called()


def test_calculate_cost_precision(model_config_openai):
    usage = TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)
    cost = calculate_cost(usage, model_config_openai)
    assert len(str(cost).split(".")[-1]) <= 6
