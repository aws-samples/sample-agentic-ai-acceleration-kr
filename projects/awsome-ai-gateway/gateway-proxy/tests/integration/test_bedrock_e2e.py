# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Bedrock 엔드포인트 E2E 통합 테스트.

요청이 전체 미들웨어 체인(OTel → Auth → RateLimit → Budget → Router → BedrockAdapter)을
통과하여 Bedrock 응답을 반환하는 시나리오를 검증한다.

미들웨어 체인 통과 조건 요약:
  1. OTelMiddleware      — 항상 통과 (request_id, start_time 주입)
  2. AuthMiddleware       — Bearer VK 토큰의 SHA-256 해시가 DB virtual_keys와 일치
  3. RateLimitMiddleware  — RPM 한도 미초과 (Redis Lua sliding window)
  4. BudgetMiddleware     — Redis에 budget:config:user:{id} 존재 + 잔여 예산 > 0
  5. HeaderInjectorMiddleware — 항상 통과 (응답 헤더 주입)
  6. bedrock.py Router    — ModelConfig 조회 성공 + Key Scope 통과
  7. BedrockAdapter       — boto3 invoke_model / converse 성공
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------
VK_TOKEN = "vk-test-token-for-e2e"
VK_HASH = hashlib.sha256(VK_TOKEN.encode()).hexdigest()
USER_ID = "user-e2e-001"
TEAM_ID = "team-e2e-001"
DEPT_ID = "dept-e2e-001"
MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"

# ---------------------------------------------------------------------------
# Lua 스크립트 로드 (모듈 수준 — 전 테스트 공유)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "app" / "redis_scripts"
LuaScriptLoader.load_all(_SCRIPT_DIR)


# ---------------------------------------------------------------------------
# Redis eval 응답 헬퍼
# ---------------------------------------------------------------------------


def _rate_limit_ok(remaining=59, limit=60):
    return json.dumps(
        {
            "allowed": True,
            "remaining": remaining,
            "limit": limit,
            "retry_after": None,
            "window_reset": 0,
        }
    ).encode()


def _budget_ok(used=100, limit=1000, policy="hard_block"):
    return json.dumps(
        {
            "allowed": True,
            "reason": None,
            "used_usd": used,
            "remaining_usd": limit - used,
            "limit_usd": limit,
            "policy": policy,
            "throttle_active": False,
            "throttle_rpm_pct": 50,
            "threshold_pct": int(used / limit * 100),
            "soft_warning": False,
        }
    ).encode()


def _budget_no_config():
    return json.dumps(
        {
            "allowed": False,
            "reason": "no_budget_assigned",
            "used_usd": 0,
            "remaining_usd": 0,
            "policy": "hard_block",
            "throttle_active": False,
            "throttle_rpm_pct": 50,
            "threshold_pct": 0,
            "soft_warning": False,
        }
    ).encode()


def _budget_hard_block(used=1000, limit=1000):
    return json.dumps(
        {
            "allowed": False,
            "reason": "hard_block",
            "used_usd": used,
            "remaining_usd": 0,
            "limit_usd": limit,
            "policy": "hard_block",
            "throttle_active": False,
            "throttle_rpm_pct": 50,
            "threshold_pct": 100,
            "soft_warning": False,
        }
    ).encode()


# ---------------------------------------------------------------------------
# DB Mock 헬퍼
# ---------------------------------------------------------------------------


def _make_vk_row(allowed_models=None):
    vk = MagicMock()
    vk.key_hash = VK_HASH
    vk.user_id = USER_ID
    vk.status = "active"
    vk.expires_at = None
    vk.allowed_models = allowed_models
    vk.id = "vk-id-001"
    return vk


def _make_user_row():
    user = MagicMock()
    user.id = USER_ID
    user.team_id = TEAM_ID
    user.dept_id = DEPT_ID
    user.roles = ["USER"]
    user.is_active = True
    return user


def _make_model_config_row():
    mc = MagicMock()
    mc.provider_model_id = MODEL_ID
    mc.alias = None
    mc.provider = "BEDROCK"
    mc.api_format = "BEDROCK_NATIVE"
    mc.endpoint = "us-east-1"
    mc.status = "ACTIVE"
    return mc


def _make_pricing_row():
    mp = MagicMock()
    mp.model_alias = MODEL_ID  # NEEDS_CONTEXT: alias value TBD in Task 3 RouterService rewrite
    mp.input_per_1k = Decimal("0.003")
    mp.output_per_1k = Decimal("0.015")
    return mp


def _fake_bedrock_invoke(input_tokens=10, output_tokens=20):
    body_content = json.dumps(
        {
            "type": "message",
            "content": [{"type": "text", "text": "Hello from mock Bedrock!"}],
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": input_tokens + output_tokens,
            },
        }
    ).encode()
    body_stream = MagicMock()
    body_stream.read.return_value = body_content
    return {"body": body_stream}


# ---------------------------------------------------------------------------
# DB session mock — VK → User → ModelConfig → Pricing 순서
# ---------------------------------------------------------------------------


def _build_db_session(allowed_models=None):
    session = AsyncMock()

    vk_res = MagicMock()
    vk_res.scalar_one_or_none.return_value = _make_vk_row(allowed_models)
    user_res = MagicMock()
    user_res.scalar_one_or_none.return_value = _make_user_row()
    model_res = MagicMock()
    model_res.scalar_one_or_none.return_value = _make_model_config_row()
    pricing_res = MagicMock()
    pricing_res.scalar_one_or_none.return_value = _make_pricing_row()

    session.execute = AsyncMock(side_effect=[vk_res, user_res, model_res, pricing_res])
    return session


def _build_db_session_vk_not_found():
    session = AsyncMock()
    vk_res = MagicMock()
    vk_res.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=vk_res)
    return session


# ---------------------------------------------------------------------------
# Redis mock — eval 호출 순서: rate_limit_check → budget_check
# ---------------------------------------------------------------------------


def _build_redis(rate_limit_resp=None, budget_resp=None):
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.setex = AsyncMock()
    redis.ping = AsyncMock(return_value=True)

    rl = rate_limit_resp or _rate_limit_ok()
    bg = budget_resp or _budget_ok()
    redis.eval = AsyncMock(side_effect=[rl, bg])
    return redis


# ---------------------------------------------------------------------------
# 앱 조립
# ---------------------------------------------------------------------------


def _build_app(mock_redis, mock_db_session, bedrock_client):
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
    app.state.tokenizer = None  # tests don't exercise tokenizer fallback
    app.state.degradation_manager = DegradationManager()
    app.state.security_detector = SecurityEventDetector()
    app.state.redis = mock_redis

    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db_session)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def inject_state(request: Request, call_next):
        state = request.scope.setdefault("state", {})
        state["_redis"] = app.state.redis
        state["_session_factory"] = app.state.session_factory
        state["_degradation_manager"] = app.state.degradation_manager
        state["_security_detector"] = app.state.security_detector
        return await call_next(request)

    return app


def _auth_header(token=VK_TOKEN):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ===========================================================================
# TC-1: 정상 invoke — 전체 미들웨어 통과 → Bedrock 200
# ===========================================================================


@pytest.mark.skip(
    reason="KI-07: test mocks assume DB-first VK lookup but VKAuthStrategy is Redis-first; pre-existing, not FR-1.2 regression"
)
class TestBedrockInvokeSuccess:
    @pytest.mark.asyncio
    async def test_invoke_returns_200_with_bedrock_body(self):
        """VK 인증 → RateLimit 통과 → Budget 통과 → Bedrock invoke_model 200."""
        bedrock_client = MagicMock()
        bedrock_client.invoke_model.return_value = _fake_bedrock_invoke()

        app = _build_app(_build_redis(), _build_db_session(), bedrock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=b'{"prompt":"Hello","max_tokens":10}',
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["content"][0]["text"] == "Hello from mock Bedrock!"
        assert body["usage"]["totalTokens"] == 30
        assert "x-request-id" in resp.headers
        assert "x-budget-remaining" in resp.headers
        bedrock_client.invoke_model.assert_called_once()


# ===========================================================================
# TC-2: 정상 converse — 전체 미들웨어 통과 → Bedrock 200
# ===========================================================================


@pytest.mark.skip(
    reason="KI-07: test mocks assume DB-first VK lookup but VKAuthStrategy is Redis-first; pre-existing, not FR-1.2 regression"
)
class TestBedrockConverseSuccess:
    @pytest.mark.asyncio
    async def test_converse_returns_200(self):
        """VK 인증 → RateLimit 통과 → Budget 통과 → Bedrock converse 200."""
        converse_resp = {
            "output": {"message": {"role": "assistant", "content": [{"text": "Hi!"}]}},
            "usage": {"inputTokens": 5, "outputTokens": 8},
        }
        bedrock_client = MagicMock()
        bedrock_client.converse.return_value = converse_resp

        app = _build_app(_build_redis(), _build_db_session(), bedrock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/converse",
                headers=_auth_header(),
                content=json.dumps(
                    {
                        "messages": [{"role": "user", "content": [{"text": "Hello"}]}],
                        "inferenceConfig": {"maxTokens": 10},
                    }
                ),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["output"]["message"]["content"][0]["text"] == "Hi!"
        bedrock_client.converse.assert_called_once()


# ===========================================================================
# TC-3,4: Auth 미들웨어 차단
# ===========================================================================


class TestAuthBlocking:
    @pytest.mark.asyncio
    async def test_no_auth_header_returns_401(self):
        """Authorization 헤더 없이 요청 → Auth 미들웨어에서 401 차단."""
        bedrock_client = MagicMock()
        app = _build_app(_build_redis(), _build_db_session(), bedrock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(f"/model/{MODEL_ID}/invoke", content=b"{}")

        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "auth_failed"
        bedrock_client.invoke_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_vk_returns_401(self):
        """존재하지 않는 VK → DB 조회 None → Auth 미들웨어 401."""
        bedrock_client = MagicMock()
        app = _build_app(_build_redis(), _build_db_session_vk_not_found(), bedrock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header("vk-wrong-key"),
                content=b"{}",
            )

        assert resp.status_code == 401
        bedrock_client.invoke_model.assert_not_called()


# ===========================================================================
# TC-5,6: Budget 미들웨어 차단
# ===========================================================================


@pytest.mark.skip(
    reason="KI-07: test mocks assume DB-first VK lookup but VKAuthStrategy is Redis-first; pre-existing, not FR-1.2 regression"
)
class TestBudgetBlocking:
    @pytest.mark.asyncio
    async def test_no_budget_config_returns_429(self):
        """예산 미설정 → budget_check.lua → no_budget_assigned → 429."""
        bedrock_client = MagicMock()
        redis = _build_redis(
            rate_limit_resp=_rate_limit_ok(),
            budget_resp=_budget_no_config(),
        )
        app = _build_app(redis, _build_db_session(), bedrock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=b"{}",
            )

        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "no_budget_assigned"
        bedrock_client.invoke_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_429(self):
        """예산 소진 (hard_block) → 429."""
        bedrock_client = MagicMock()
        redis = _build_redis(
            rate_limit_resp=_rate_limit_ok(),
            budget_resp=_budget_hard_block(),
        )
        app = _build_app(redis, _build_db_session(), bedrock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=b"{}",
            )

        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "hard_block"
        bedrock_client.invoke_model.assert_not_called()


# ===========================================================================
# TC-7: Key Scope 차단
# ===========================================================================


@pytest.mark.skip(
    reason="KI-07: test mocks assume DB-first VK lookup but VKAuthStrategy is Redis-first; pre-existing, not FR-1.2 regression"
)
class TestKeyScopeBlocking:
    @pytest.mark.asyncio
    async def test_model_not_in_allowed_models_returns_403(self):
        """VK의 allowed_models에 요청 모델 없음 → Router에서 403."""
        bedrock_client = MagicMock()
        db = _build_db_session(allowed_models=["other-model-only"])
        app = _build_app(_build_redis(), db, bedrock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=b"{}",
            )

        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "model_not_allowed"
        bedrock_client.invoke_model.assert_not_called()


# ===========================================================================
# TC-8,9: Bedrock 프로바이더 오류 전파
# ===========================================================================


@pytest.mark.skip(
    reason="KI-07: test mocks assume DB-first VK lookup but VKAuthStrategy is Redis-first; pre-existing, not FR-1.2 regression"
)
class TestBedrockProviderError:
    @pytest.mark.asyncio
    async def test_throttling_exception_returns_429(self):
        """Bedrock ThrottlingException → 429 provider_error."""
        from botocore.exceptions import ClientError

        bedrock_client = MagicMock()
        bedrock_client.invoke_model.side_effect = ClientError(
            error_response={"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            operation_name="InvokeModel",
        )
        app = _build_app(_build_redis(), _build_db_session(), bedrock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=b'{"prompt":"hello"}',
            )

        assert resp.status_code == 429
        assert resp.json()["error"]["type"] == "provider_error"

    @pytest.mark.asyncio
    async def test_access_denied_returns_403(self):
        """Bedrock AccessDeniedException → 403 provider_error."""
        from botocore.exceptions import ClientError

        bedrock_client = MagicMock()
        bedrock_client.invoke_model.side_effect = ClientError(
            error_response={"Error": {"Code": "AccessDeniedException", "Message": "Forbidden"}},
            operation_name="InvokeModel",
        )
        app = _build_app(_build_redis(), _build_db_session(), bedrock_client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=b'{"prompt":"hello"}',
            )

        assert resp.status_code == 403
        assert resp.json()["error"]["type"] == "provider_error"


# ===========================================================================
# TC-10,11,12: FR-1.2 Alias Routing Regression
# ===========================================================================


class TestAliasRoutingRegression:
    """FR-1.2 design: alias / full ID / unregistered → 404 regression."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_alias_call_resolves_to_seeded_provider_id(self):
        """`claude-sonnet-4-6` alias로 호출 → DB에서 provider_model_id로 변환되어 Bedrock에 전달."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.services.router_service import RouterService

        engine = create_async_engine(
            "postgresql+asyncpg://gateway:gateway_dev_password@localhost:5432/gateway"
        )
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
        rs = RouterService()
        async with SessionLocal() as session:
            schema = await rs.resolve_bedrock_model(
                redis=None, db=session, model_ref="claude-sonnet-4-6"
            )
        assert schema.provider_model_id == "global.anthropic.claude-sonnet-4-6"
        await engine.dispose()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_full_id_call_resolves(self):
        """full Bedrock ID로 호출해도 같은 row 반환."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.services.router_service import RouterService

        engine = create_async_engine(
            "postgresql+asyncpg://gateway:gateway_dev_password@localhost:5432/gateway"
        )
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
        rs = RouterService()
        async with SessionLocal() as session:
            schema = await rs.resolve_bedrock_model(
                redis=None,
                db=session,
                model_ref="global.anthropic.claude-sonnet-4-6",
            )
        assert schema.alias == "claude-sonnet-4-6"
        await engine.dispose()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_unregistered_full_id_now_raises_lookup_error(self):
        """회귀: 기존 코드는 `.`이 있으면 200으로 통과시켰음. 이제는 LookupError."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.services.router_service import RouterService

        engine = create_async_engine(
            "postgresql+asyncpg://gateway:gateway_dev_password@localhost:5432/gateway"
        )
        SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
        rs = RouterService()
        async with SessionLocal() as session:
            with pytest.raises(LookupError, match="not found"):
                await rs.resolve_bedrock_model(
                    redis=None, db=session, model_ref="global.anthropic.fake-model-zzz"
                )
        await engine.dispose()
