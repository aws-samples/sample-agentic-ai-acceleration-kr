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
from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    Role,
)
from app.security.event_detector import SecurityEventDetector
from app.services.cost_recorder import CostRecorder
from app.services.lua_loader import LuaScriptLoader


def _aws_credentials_available() -> bool:
    """Return True only when boto3 can actually resolve AND validate AWS credentials.

    ⚠️ `boto3.client(...)` alone NEVER raises without creds (creds resolve lazily at
    call time), so the old guard always returned True → these live tests never skipped
    and hard-failed in CI (DEVLOG §68.3). Make the gate real by calling STS
    GetCallerIdentity with a short timeout: no creds / offline → skip.
    """
    try:
        from botocore.config import Config

        sts = boto3.client(
            "sts",
            region_name="ap-northeast-2",
            config=Config(connect_timeout=3, read_timeout=3, retries={"max_attempts": 1}),
        )
        sts.get_caller_identity()
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


def _auth_context_json(allowed_models=None):
    """Serialized AuthContext for the Redis-first VK cache-hit path (key:cache:vk:*)."""
    return AuthContext(
        user_id=USER_ID,
        team_id=TEAM_ID,
        dept_id=DEPT_ID,
        roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
        key_id=None,
        allowed_models=allowed_models,
        allowed_clients=None,
        sso_subject=None,
    ).model_dump_json()


def _model_config_json():
    """Serialized ModelConfigSchema for RouterService's model:{ref} cache hit.

    실제 Bedrock 모델 ID/리전을 그대로 사용 — Router 는 캐시 히트로 DB 를 건너뛰고,
    반환된 provider_model_id 로 실제 Bedrock 을 호출한다.
    """
    return ModelConfigSchema(
        provider_model_id=MODEL_ID,
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint=REGION,
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    ).model_dump_json()


def _budget_user_passthrough():
    # USER config 미설정 → pass-through (Q 정책). config_present=false.
    return {
        "allowed": True, "reason": None, "used_usd": 0, "remaining_usd": 0,
        "limit_usd": 0, "policy": "hard_block", "throttle_active": False,
        "throttle_rpm_pct": 50, "threshold_pct": 0, "soft_warning": False,
        "scope": "user", "config_present": False, "app_clients": [],
    }


def _budget_team_ok(used=50, limit=1000):
    return {
        "allowed": True, "reason": None, "used_usd": used,
        "remaining_usd": limit - used, "limit_usd": limit, "policy": "hard_block",
        "throttle_active": False, "throttle_rpm_pct": 50,
        "threshold_pct": int(used / limit * 100), "soft_warning": False,
        "scope": "team", "config_present": True, "app_clients": [],
    }


def _build_redis():
    """Redis-first key-aware / scope-aware mock (test_bedrock_e2e.py 와 동일 구조)."""
    redis = AsyncMock()
    auth_json = _auth_context_json()
    model_json = _model_config_json()
    cache_key = f"key:cache:vk:{VK_HASH}"
    model_key = f"model:{MODEL_ID}"

    def _get(key, *args, **kwargs):
        if key == cache_key:
            return auth_json  # VKAuthStrategy 캐시 히트
        if key == model_key:
            return model_json  # RouterService 모델 캐시 히트
        return None  # rl:config:* miss → DB → 빈 결과 → 무제한

    redis.get = AsyncMock(side_effect=_get)
    redis.exists = AsyncMock(return_value=1)
    redis.set = AsyncMock()
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    redis.ping = AsyncMock(return_value=True)

    user_resp = _budget_user_passthrough()
    team_resp = _budget_team_ok()

    def _eval(script, numkeys, *argv):
        label = argv[numkeys] if len(argv) > numkeys else ""
        if label == "team":
            return json.dumps(team_resp).encode()
        return json.dumps(user_resp).encode()

    redis.eval = AsyncMock(side_effect=_eval)
    return redis


def _build_db_session():
    # Redis-first: DB 는 (1) VK 캐시 히트 시 User.is_active 재확인, (2) rate-limit
    # config 조회(빈 결과=무제한) 두 경로에서만 접근. generic result 로 둘 다 충족.
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = True
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
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
    # NOTE: x-ratelimit-remaining 은 Redis-DOWN in-memory fallback 경로에서만
    # 방출된다(rate_limit.py:119/124). 정상 /model/ 경로는 방출하지 않으므로
    # 어서션에서 제외 (KI-07 / DEVLOG §68.3).
    assert "x-request-id" in resp.headers
    assert "x-budget-remaining" in resp.headers


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
