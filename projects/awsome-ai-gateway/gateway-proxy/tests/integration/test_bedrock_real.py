# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Bedrock 실제 호출 E2E 테스트.

mock 없이 실제 AWS Bedrock를 호출하여 전체 미들웨어 체인을 검증한다.
  - 모델: global.anthropic.claude-sonnet-4-6 (ap-northeast-2)
  - 인증: VK mock (DB/Redis mock) — Bedrock만 실제 호출
  - 미들웨어 체인: OTel → Auth → RateLimit → Budget → Router → BedrockAdapter(실제)

실행 전제: AWS SSO 로그인 완료 (aws sts get-caller-identity 성공)
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.degradation.manager import DegradationManager
from app.middleware.auth import AuthMiddleware
from app.middleware.budget import BudgetMiddleware
from app.middleware.otel import HeaderInjectorMiddleware, OTelMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.providers.bedrock_adapter import BedrockAdapter
from app.providers.registry import ProviderRegistry
from app.routers import bedrock
from app.schemas.domain import ProviderType
from app.security.event_detector import SecurityEventDetector
from app.services.cost_recorder import CostRecorder
from app.services.lua_loader import LuaScriptLoader


def _aws_credentials_available() -> bool:
    """Return True only when boto3 can resolve AWS credentials without errors."""
    try:
        boto3.client("bedrock-runtime", region_name="ap-northeast-2")
        return True
    except Exception:
        return False


_SKIP_REAL = pytest.mark.skipif(
    not _aws_credentials_available(),
    reason=(
        "Real AWS credentials not available (botocore[crt] missing or"
        " credentials unresolvable) — skipping live Bedrock tests"
    ),
)

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
REGION = "ap-northeast-2"
MODEL_ID = "global.anthropic.claude-sonnet-4-6"

VK_TOKEN = "vk-real-bedrock-test"
VK_HASH = hashlib.sha256(VK_TOKEN.encode()).hexdigest()
USER_ID = "user-real-001"
TEAM_ID = "team-real-001"
DEPT_ID = "dept-real-001"

_SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "app" / "redis_scripts"
LuaScriptLoader.load_all(_SCRIPT_DIR)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _rl_ok():
    return json.dumps(
        {
            "allowed": True,
            "remaining": 59,
            "limit": 60,
            "retry_after": None,
            "window_reset": 0,
        }
    ).encode()


def _budget_ok():
    return json.dumps(
        {
            "allowed": True,
            "reason": None,
            "used_usd": 50,
            "remaining_usd": 950,
            "limit_usd": 1000,
            "policy": "hard_block",
            "throttle_active": False,
            "throttle_rpm_pct": 50,
            "threshold_pct": 5,
            "soft_warning": False,
        }
    ).encode()


def _build_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.setex = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    redis.eval = AsyncMock(side_effect=[_rl_ok(), _budget_ok()])
    return redis


def _build_db_session():
    session = AsyncMock()

    vk = MagicMock()
    vk.key_hash = VK_HASH
    vk.user_id = USER_ID
    vk.status = "active"
    vk.expires_at = None
    vk.allowed_models = None
    vk.id = "vk-real-001"
    vk_res = MagicMock()
    vk_res.scalar_one_or_none.return_value = vk

    user = MagicMock()
    user.id = USER_ID
    user.team_id = TEAM_ID
    user.dept_id = DEPT_ID
    user.roles = ["USER"]
    user.is_active = True
    user_res = MagicMock()
    user_res.scalar_one_or_none.return_value = user

    # ModelConfig — 실제 모델 ID & 리전
    mc = MagicMock()
    mc.provider_model_id = MODEL_ID
    mc.alias = None
    mc.provider = "BEDROCK"
    mc.api_format = "BEDROCK_NATIVE"
    mc.endpoint = REGION
    mc.status = "ACTIVE"
    mc_res = MagicMock()
    mc_res.scalar_one_or_none.return_value = mc

    pricing = MagicMock()
    pricing.model_alias = MODEL_ID  # NEEDS_CONTEXT: alias value TBD in Task 3 RouterService rewrite
    pricing.input_per_1k = Decimal("0.003")
    pricing.output_per_1k = Decimal("0.015")
    pr_res = MagicMock()
    pr_res.scalar_one_or_none.return_value = pricing

    session.execute = AsyncMock(side_effect=[vk_res, user_res, mc_res, pr_res])
    return session


def _build_app(bedrock_client, mock_redis, mock_db):
    app = FastAPI()
    app.add_middleware(HeaderInjectorMiddleware)
    app.add_middleware(BudgetMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(OTelMiddleware)
    app.include_router(bedrock.router)

    registry = ProviderRegistry()
    registry.register(ProviderType.BEDROCK, BedrockAdapter(bedrock_client))
    app.state.provider_registry = registry

    cost_recorder = MagicMock(spec=CostRecorder)
    cost_recorder.finalize = AsyncMock()
    app.state.cost_recorder = cost_recorder
    app.state.tokenizer = None
    app.state.degradation_manager = DegradationManager()
    app.state.security_detector = SecurityEventDetector()
    app.state.redis = mock_redis

    sf = MagicMock()
    sf.return_value.__aenter__ = AsyncMock(return_value=mock_db)
    sf.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.session_factory = sf

    @app.middleware("http")
    async def inject_state(request: Request, call_next):
        state = request.scope.setdefault("state", {})
        state["_redis"] = mock_redis
        state["_session_factory"] = app.state.session_factory
        state["_degradation_manager"] = app.state.degradation_manager
        state["_security_detector"] = app.state.security_detector
        return await call_next(request)

    return app


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------


@_SKIP_REAL
@pytest.mark.asyncio
async def test_real_bedrock_invoke():
    """실제 Bedrock invoke_model 호출 — 전체 미들웨어 체인 통과."""
    bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)
    app = _build_app(bedrock_client, _build_redis(), _build_db_session())

    request_body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 20,
            "messages": [{"role": "user", "content": "What is 2+2? Answer with just the number."}],
        }
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            f"/model/{MODEL_ID}/invoke",
            headers={"Authorization": f"Bearer {VK_TOKEN}", "Content-Type": "application/json"},
            content=request_body,
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()

    # Bedrock 응답 구조 검증
    assert "content" in body
    assert len(body["content"]) > 0
    assert body["content"][0]["type"] == "text"
    assert len(body["content"][0]["text"]) > 0
    print(f'\n[invoke] response: "{body["content"][0]["text"]}"')
    print(f"[invoke] usage: {body.get('usage', {})}")

    # 미들웨어 헤더 검증
    assert "x-request-id" in resp.headers
    assert "x-budget-remaining" in resp.headers
    assert "x-ratelimit-remaining" in resp.headers


@_SKIP_REAL
@pytest.mark.asyncio
async def test_real_bedrock_converse():
    """실제 Bedrock converse 호출 — 전체 미들웨어 체인 통과."""
    bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)
    app = _build_app(bedrock_client, _build_redis(), _build_db_session())

    request_body = json.dumps(
        {
            "messages": [
                {"role": "user", "content": [{"text": "What is 3+3? Answer with just the number."}]}
            ],
            "inferenceConfig": {"maxTokens": 20},
        }
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            f"/model/{MODEL_ID}/converse",
            headers={"Authorization": f"Bearer {VK_TOKEN}", "Content-Type": "application/json"},
            content=request_body,
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()

    # Converse 응답 구조 검증
    assert "output" in body
    assert "message" in body["output"]
    text = body["output"]["message"]["content"][0]["text"]
    assert len(text) > 0
    print(f'\n[converse] response: "{text}"')
    print(f"[converse] usage: {body.get('usage', {})}")
    print(f"[converse] stopReason: {body.get('stopReason', 'N/A')}")

    # 미들웨어 헤더 검증
    assert "x-request-id" in resp.headers
    assert "x-budget-remaining" in resp.headers
