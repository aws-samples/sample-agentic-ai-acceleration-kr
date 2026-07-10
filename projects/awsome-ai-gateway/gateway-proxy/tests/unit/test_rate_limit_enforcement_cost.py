# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""FR-4.6 enforcement 레이어의 CPM/CPH pre-reserve 통합 유닛 테스트."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)
from app.services.rate_limit_enforcement import _estimate_cost, enforce_rate_limits


def _model(input_per_1k: str = "0.003", output_per_1k: str = "0.015") -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id="anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="",
        pricing=ModelPricingSchema(
            input_per_1k=Decimal(input_per_1k),
            output_per_1k=Decimal(output_per_1k),
        ),
        status=ModelStatus.ACTIVE,
    )


def test_estimate_cost_uses_input_and_max_output():
    cost = _estimate_cost(_model(), estimated_input=2000, max_output=1000)
    # input: 2000/1000 * 0.003 = 0.006
    # output: 1000/1000 * 0.015 = 0.015
    assert cost == Decimal("0.021000")


@pytest.mark.asyncio
async def test_enforce_cpm_exceeded_returns_429(monkeypatch, mock_redis):
    """CPM 초과 시 429 JSONResponse (RPM/TPM은 통과 가정)."""
    # RPM/TPM 통과 mock
    async def _rpm_ok(*args, **kwargs):
        from app.schemas.domain import RateLimitResult

        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _tpm_ok(*args, **kwargs):
        from app.schemas.domain import RateLimitResult

        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    # CPM 거부 mock
    async def _cost_fail(*args, **kwargs):
        from app.schemas.domain import CostLimitResult

        return CostLimitResult(
            allowed=False,
            scope="USER",
            limit_type="cpm",
            limit=Decimal("0.5"),
            remaining=Decimal("0"),
            retry_after=60,
            reserved_cost=Decimal("0"),
        )

    from app.services import rate_limit_enforcement as enf

    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_rpm", _rpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_tpm", _tpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "reserve_cost", _cost_fail)

    # config loader — USER CPM=0.5 설정
    async def _load_limits(**_kwargs):
        from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

        return AllScopeLimits(
            user=ScopeLimits(rpm=1000, tpm=1_000_000, cpm=Decimal("0.5"), cph=None),
            team=ScopeLimits(),
            global_=ScopeLimits(),
        )

    monkeypatch.setattr(enf, "load_all_scope_limits", _load_limits)

    auth = AuthContext(
        user_id="u-1",
        user_name="u",
        team_id="t-1",
        dept_id="d-1",
        roles=["USER"],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    state: dict = {}

    resp = await enforce_rate_limits(
        redis=mock_redis,
        auth_context=auth,
        model_config=_model(),
        body={"max_tokens": 1000},
        state=state,
        request_id="req-1",
    )

    assert resp is not None
    assert resp.status_code == 429
    body = json.loads(bytes(resp.body).decode())
    assert body["error"]["code"] == "user_cpm_exceeded"
    assert body["error"]["scope"] == "USER"
    assert body["error"]["limit_type"] == "cpm"
    assert resp.headers["Retry-After"] == "60"
    # state에 cost_reserved 주입되지 않음 (거부됨)
    assert "rate_limit_state" not in state


@pytest.mark.asyncio
async def test_enforce_cpm_pass_injects_cost_reserved(monkeypatch, mock_redis):
    """CPM 통과 시 state['rate_limit_state']['cost_reserved']에 예약액 주입."""
    from app.schemas.domain import CostLimitResult, RateLimitResult

    async def _rpm_ok(*args, **kwargs):
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _tpm_ok(*args, **kwargs):
        return RateLimitResult(allowed=True, remaining=-1, limit=-1)

    async def _cost_ok(*args, **kwargs):
        return CostLimitResult(allowed=True, reserved_cost=Decimal("0.021"))

    from app.services import rate_limit_enforcement as enf

    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_rpm", _rpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "check_multi_scope_tpm", _tpm_ok)
    monkeypatch.setattr(enf.RateLimitService, "reserve_cost", _cost_ok)

    async def _load_limits(**_kwargs):
        from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

        return AllScopeLimits(
            user=ScopeLimits(cpm=Decimal("1.0")),
            team=ScopeLimits(),
            global_=ScopeLimits(),
        )

    monkeypatch.setattr(enf, "load_all_scope_limits", _load_limits)

    auth = AuthContext(
        user_id="u-1",
        user_name="u",
        team_id="t-1",
        dept_id="d-1",
        roles=["USER"],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    state: dict = {}
    resp = await enforce_rate_limits(
        redis=mock_redis,
        auth_context=auth,
        model_config=_model(),
        body={"max_tokens": 500},
        state=state,
        request_id="req-2",
    )

    assert resp is None
    assert state["rate_limit_state"]["cost_reserved"] == Decimal("0.021")
