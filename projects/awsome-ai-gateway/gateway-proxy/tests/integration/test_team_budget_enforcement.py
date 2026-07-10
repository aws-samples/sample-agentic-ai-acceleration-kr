# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""TEAM 예산 enforcement (C-1) + USER 정책 (Q) 통합 테스트.

VKAuthStrategy 는 Redis-first 이므로 `key:cache:vk:{hash}` 를 직접 주입해
DB 없이 인증을 통과시킨다 (KI-07 우회 — 새 테스트는 Redis-cache 경로만 사용).

시나리오:
  1. test_team_unset_user_unset_returns_429   — TEAM 미설정   → 429 team_budget_unset
  2. test_team_set_user_unset_passes          — TEAM $100 설정, USER 미설정 → 비-429 (예산 통과)
  3. test_team_exceeded_returns_429           — TEAM 한도 초과 → 429 team_budget_exceeded
  4. test_user_exceeded_team_ok_returns_429_user — USER 한도 초과 (TEAM 여유) → 429 user_budget_exceeded
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
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
from app.routers import bedrock
from app.schemas.domain import AuthType, ProviderType, Role
from app.security.event_detector import SecurityEventDetector
from app.services.cost_recorder import CostRecorder
from app.services.lua_loader import LuaScriptLoader

# ---------------------------------------------------------------------------
# Lua 스크립트 로드 (모듈 수준 — 전 테스트 공유)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = (
    Path(__file__).resolve().parent.parent.parent / "src" / "app" / "redis_scripts"
)
LuaScriptLoader.load_all(_SCRIPT_DIR)

# ---------------------------------------------------------------------------
# 공통 상수
# ---------------------------------------------------------------------------
VK_TOKEN = "vk-budget-enforcement-test"
VK_HASH = hashlib.sha256(VK_TOKEN.encode()).hexdigest()
USER_ID = "user-budget-e2e-001"
TEAM_ID = "team-budget-e2e-001"
MODEL_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
PERIOD = datetime.now(tz=timezone.utc).strftime("%Y-%m")

# Redis key names (hash-tag 형식 — budget_service.py 와 동일)
_USER_USAGE_KEY = f"budget:user:{{{USER_ID}}}:{PERIOD}"
_USER_CONFIG_KEY = f"budget:config:user:{{{USER_ID}}}"
_TEAM_USAGE_KEY = f"budget:team:{{{TEAM_ID}}}:{PERIOD}"
_TEAM_CONFIG_KEY = f"budget:config:team:{{{TEAM_ID}}}"
_VK_CACHE_KEY = f"key:cache:vk:{VK_HASH}"


# ---------------------------------------------------------------------------
# AuthContext 캐시 JSON — Redis-first VK 인증에 필요한 최소 페이로드
# ---------------------------------------------------------------------------
def _auth_context_json() -> str:
    return json.dumps(
        {
            "user_id": USER_ID,
            "team_id": TEAM_ID,
            "dept_id": "dept-001",
            "roles": [Role.USER.value],
            "auth_type": AuthType.VIRTUAL_KEY.value,
            "key_id": None,
            "allowed_models": None,
        }
    )


# ---------------------------------------------------------------------------
# Lua eval 응답 빌더 — budget_check.lua 가 반환하는 JSON 형태
# ---------------------------------------------------------------------------

# Lua 새 contract: scope 당 1개 EVAL 응답. `config_present` 필드로
# 미설정(pass/deny)과 설정됨(limit 체크)을 구분.

def _resp(*, allowed: bool, reason=None, scope: str,
          used_usd=0.0, remaining_usd=0.0, limit_usd=0.0,
          policy="hard_block", throttle_active=False, throttle_rpm_pct=50,
          threshold_pct=0, soft_warning=False, config_present=True) -> bytes:
    return json.dumps(
        {
            "allowed": allowed,
            "reason": reason,
            "used_usd": used_usd,
            "remaining_usd": remaining_usd,
            "limit_usd": limit_usd,
            "policy": policy,
            "throttle_active": throttle_active,
            "throttle_rpm_pct": throttle_rpm_pct,
            "threshold_pct": threshold_pct,
            "soft_warning": soft_warning,
            "scope": scope,
            "config_present": config_present,
        }
    ).encode()


def _budget_team_unset() -> tuple[bytes, bytes]:
    """C-1: TEAM 미설정(config_present=False) → team_budget_unset."""
    return (
        _resp(allowed=True, scope="user", config_present=False),
        _resp(allowed=True, scope="team", config_present=False),
    )


def _budget_team_ok(used: float = 10.0, limit: float = 100.0) -> tuple[bytes, bytes]:
    """TEAM 통과 — 잔여 예산 있음 (USER 미설정 pass-through)."""
    return (
        _resp(allowed=True, scope="user", config_present=False),
        _resp(
            allowed=True, scope="team",
            used_usd=used, remaining_usd=limit - used, limit_usd=limit,
            threshold_pct=int(used / limit * 100),
        ),
    )


def _budget_team_exceeded(used: float = 100.0, limit: float = 100.0) -> tuple[bytes, bytes]:
    """TEAM 한도 초과 → team_budget_exceeded."""
    return (
        _resp(allowed=True, scope="user", config_present=False),
        _resp(
            allowed=False, reason="team_budget_exceeded", scope="team",
            used_usd=used, remaining_usd=0.0, limit_usd=limit, threshold_pct=100,
        ),
    )


def _budget_user_exceeded(used: float = 50.0, limit: float = 50.0) -> tuple[bytes, bytes]:
    """USER 한도 초과 → USER 단계에서 차단. TEAM EVAL 은 호출되지 않음."""
    return (
        _resp(
            allowed=False, reason="user_budget_exceeded", scope="user",
            used_usd=used, remaining_usd=0.0, limit_usd=limit, threshold_pct=100,
        ),
        _resp(allowed=True, scope="team"),   # unused
    )


# ---------------------------------------------------------------------------
# Redis mock 빌더
#
# auth_service.py VKAuthStrategy (Redis-first):
#   1) redis.get("key:cache:vk:{hash}")  → AuthContext JSON (캐시 히트)
#   2) 캐시 히트 후 db.execute(User.is_active) → True (is_active 재확인)
#
# budget_service.py:
#   3) redis.exists(user_key) → 1 (counter 이미 있다고 가정 — DB 복구 루프 스킵)
#   4) redis.eval(budget_check_lua, ...) → scenario 별 JSON
# ---------------------------------------------------------------------------

def _build_redis(budget_eval_resps) -> AsyncMock:
    """budget_eval_resps: (user_resp, team_resp) 튜플. 순차 소비."""
    redis = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    redis.set = AsyncMock()
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    redis.publish = AsyncMock()
    redis.incrbyfloat = AsyncMock()
    redis.incrby = AsyncMock()
    redis.expire = AsyncMock()

    # exists() 호출: budget_service 가 user_key 에 대해 호출 — 1 반환으로 DB 복구 루프 스킵
    redis.exists = AsyncMock(return_value=1)

    # get() 은 키 종류에 따라 다른 값 반환 — 타입 오염 방지
    _model_config_json = json.dumps(
        {
            "provider_model_id": MODEL_ID,
            "alias": MODEL_ID,
            "provider": "BEDROCK",
            "api_format": "BEDROCK_NATIVE",
            "endpoint": "",
            "pricing": {
                "input_per_1k": "0.003",
                "output_per_1k": "0.015",
                "cache_write_per_1k": "0",
                "cache_read_per_1k": "0",
            },
            "status": "ACTIVE",
            "created_at": None,
            "description": None,
        }
    ).encode()

    # RL config: unlimited (None 값) — DB 조회 없이 캐시 히트로 처리
    _rl_config_unlimited_json = json.dumps(
        {"rpm": None, "tpm": None, "cpm": None, "cph": None}
    ).encode()

    def _redis_get_side_effect(key):
        if isinstance(key, bytes):
            key = key.decode()
        if key.startswith("key:cache:vk:"):
            # VKAuthStrategy Redis-first 인증
            return _auth_context_json().encode()
        if key.startswith("model:"):
            # RouterService 모델 설정 캐시 — DB 조회 없이 캐시 히트로 처리
            return _model_config_json
        if key.startswith("rl:config:"):
            # RateLimitConfigLoader — unlimited로 캐시 히트 처리 (DB 조회 방지)
            return _rl_config_unlimited_json
        return None  # budget config exists check 등 나머지는 캐시 미스

    redis.get = AsyncMock(side_effect=_redis_get_side_effect)

    # Redis eval: USER EVAL 먼저, TEAM EVAL 그 다음. 두 응답을 순차 반환.
    user_resp, team_resp = budget_eval_resps
    _eval_mock = AsyncMock(side_effect=[user_resp, team_resp])
    setattr(redis, "eval", _eval_mock)

    pipe = MagicMock()
    pipe.incrbyfloat = MagicMock()
    pipe.incrby = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    redis.pipeline = MagicMock(return_value=pipe)
    return redis


# ---------------------------------------------------------------------------
# DB mock — auth_service.py 가 캐시 히트 후 is_active 재확인 시 사용
# (SELECT User.is_active WHERE User.id = user_id → True)
# ---------------------------------------------------------------------------

def _build_db_session_active() -> AsyncMock:
    """캐시 히트 후 is_active 재확인만 통과시키는 최소 DB mock."""
    session = AsyncMock()
    is_active_result = MagicMock()
    is_active_result.scalar_one_or_none.return_value = True
    session.execute = AsyncMock(return_value=is_active_result)
    return session


# ---------------------------------------------------------------------------
# FastAPI 앱 빌더 — 미들웨어 체인 + bedrock 라우터
# ---------------------------------------------------------------------------

def _build_app(mock_redis: AsyncMock, mock_db_session: AsyncMock) -> FastAPI:
    from app.providers.bedrock_adapter import BedrockAdapter
    from app.providers.registry import ProviderRegistry

    app = FastAPI()

    app.add_middleware(HeaderInjectorMiddleware)
    app.add_middleware(BudgetMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(OTelMiddleware)

    app.include_router(bedrock.router)

    # boto3 invoke_model 응답 형태: {"body": StreamingBody} — body.read() → bytes
    _bedrock_resp_bytes = json.dumps(
        {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    ).encode()
    _bedrock_body = MagicMock()
    _bedrock_body.read.return_value = _bedrock_resp_bytes
    bedrock_client = MagicMock()
    bedrock_client.invoke_model.return_value = {"body": _bedrock_body}

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


def _auth_header() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {VK_TOKEN}",
        "Content-Type": "application/json",
    }


def _invoke_body() -> bytes:
    return json.dumps({"prompt": "hello", "max_tokens": 10}).encode()


# ===========================================================================
# 시나리오 1: TEAM 미설정, USER 미설정 → 429 team_budget_unset
# ===========================================================================

class TestTeamUnsetUserUnset:
    async def test_team_unset_user_unset_returns_429(self):
        """C-1: TEAM 예산 config 없음 → Lua → team_budget_unset → 429."""
        redis = _build_redis(_budget_team_unset())
        db = _build_db_session_active()
        app = _build_app(redis, db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=_invoke_body(),
            )

        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == "team_budget_unset"


# ===========================================================================
# 시나리오 2: TEAM $100 설정, USER 미설정 → 예산 통과 (non-429)
# ===========================================================================

class TestTeamSetUserUnset:
    async def test_team_set_user_unset_passes(self):
        """Q: USER config 없음 = pass-through. TEAM config 있음 → 예산 통과.

        라우터까지 진행하므로 status != 429 를 확인. 실제 Bedrock/DB 없이
        404/400/500 이 될 수 있지만, BudgetMiddleware 는 차단하지 않아야 한다.
        """
        redis = _build_redis(_budget_team_ok(used=10.0, limit=100.0))
        db = _build_db_session_active()
        app = _build_app(redis, db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=_invoke_body(),
            )

        # BudgetMiddleware 가 차단하면 429, 차단 안 하면 라우터가 처리해 다른 코드 반환
        assert resp.status_code != 429, (
            f"Expected budget to pass (not 429), got {resp.status_code}: {resp.text}"
        )
        if resp.status_code != 200:
            body = resp.json()
            assert body.get("error", {}).get("type") != "budget_exceeded", (
                f"Request was budget-blocked: {resp.text}"
            )


# ===========================================================================
# 시나리오 3: TEAM 한도 초과 → 429 team_budget_exceeded
# ===========================================================================

class TestTeamExceeded:
    async def test_team_exceeded_returns_429(self):
        """TEAM $100 / used $100 — hard_block 초과 → 429 team_budget_exceeded."""
        redis = _build_redis(_budget_team_exceeded(used=100.0, limit=100.0))
        db = _build_db_session_active()
        app = _build_app(redis, db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=_invoke_body(),
            )

        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == "team_budget_exceeded"


# ===========================================================================
# 시나리오 4: USER 한도 초과 (TEAM 여유 있음) → 429 user_budget_exceeded
# ===========================================================================

class TestUserExceededTeamOk:
    async def test_user_exceeded_team_ok_returns_429_user(self):
        """USER $50 / used $50 (한도 초과). TEAM $1000 (여유). USER 먼저 차단 → 429."""
        redis = _build_redis(_budget_user_exceeded(used=50.0, limit=50.0))
        db = _build_db_session_active()
        app = _build_app(redis, db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/model/{MODEL_ID}/invoke",
                headers=_auth_header(),
                content=_invoke_body(),
            )

        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == "user_budget_exceeded"
