# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Redis-down in-memory fallback 의 계층 집행 검증 (deepdive Q50 Phase 3).

과거 fallback 은 USER 스코프만 검사 → 한 팀이 fallback 용량을 독점 가능했다. 이제
USER→TEAM→GLOBAL 모두 근사 집행한다(fast-fail). pure-ASGI 미들웨어를 직접 구동해
degraded 상태에서 각 scope 한도가 실제로 작동하는지 잠근다.
"""

from __future__ import annotations

import json

import pytest

from app.middleware import rate_limit as rl
from app.schemas.domain import AuthType, DegradationLevel, Role


class _DM:
    """REDIS_DEGRADED 상태를 보고하는 최소 degradation manager 스텁."""

    level = DegradationLevel.REDIS_DEGRADED


def _auth(user_id="u1", team_id="t1"):
    from app.schemas.domain import AuthContext

    return AuthContext(
        user_id=user_id, team_id=team_id, dept_id="d1",
        roles=[Role.USER], auth_type=AuthType.VIRTUAL_KEY, key_id="k1",
    )


def _model_config():
    from decimal import Decimal

    from app.schemas.domain import (
        ApiFormat,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
    )

    return ModelConfigSchema(
        provider_model_id="m1", alias="m1", provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE, endpoint="us-east-1",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0"), output_per_1k=Decimal("0")),
        status=ModelStatus.ACTIVE,
    )


async def _drive(auth, *, downstream_called: list):
    """미들웨어 1회 호출. (status, headers, body) 반환. downstream 호출 여부 기록."""
    async def app(scope, receive, send):
        downstream_called.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = rl.RateLimitMiddleware(app)
    captured = {"status": None, "headers": [], "body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured["status"] = msg["status"]
            captured["headers"] = msg.get("headers", [])
        elif msg["type"] == "http.response.body":
            captured["body"] += msg.get("body", b"")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "path": "/model/foo/invoke",
        "state": {
            "auth_context": auth,
            "_degradation_manager": _DM(),
            "_redis": object(),  # redis 객체는 있으나 degraded 라 fallback 진입
            "model_config": _model_config(),
        },
    }
    await mw(scope, receive, send)
    return captured


@pytest.fixture(autouse=True)
def _fresh_limiter():
    """각 테스트마다 fallback limiter 카운터 초기화(테스트 간 격리)."""
    from app.services.rate_limit_service import InMemoryRateLimiter

    saved = rl._in_memory_limiter
    rl._in_memory_limiter = InMemoryRateLimiter(worker_count=1)  # divisor 1 → 한도 그대로
    yield
    rl._in_memory_limiter = saved


@pytest.mark.asyncio
async def test_user_scope_enforced_in_fallback(monkeypatch):
    """USER 한도(기본 60) 초과 시 fallback 이 429."""
    monkeypatch.setattr(rl, "_FALLBACK_USER_RPM", 3)
    auth = _auth()
    statuses = []
    for _ in range(5):
        called = []
        cap = await _drive(auth, downstream_called=called)
        statuses.append(cap["status"])
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    cap = await _drive(auth, downstream_called=[])
    body = json.loads(cap["body"])
    assert body["error"]["scope"] == "USER"


@pytest.mark.asyncio
async def test_team_scope_enforced_when_user_ok(monkeypatch):
    """USER 한도는 넉넉하나 TEAM 한도 초과 → 여러 사용자 합산이 TEAM 에서 막힌다.

    과거(USER-only)엔 절대 안 막혔다 — 이게 이번 수정의 핵심.
    """
    monkeypatch.setattr(rl, "_FALLBACK_USER_RPM", 1000)
    monkeypatch.setattr(rl, "_FALLBACK_TEAM_RPM", 4)
    monkeypatch.setattr(rl, "_FALLBACK_GLOBAL_RPM", 100000)
    statuses = []
    for i in range(6):
        # 같은 team, 다른 user → USER 한도엔 안 걸리고 TEAM 누적이 4 초과해야 막힘.
        cap = await _drive(_auth(user_id=f"u{i}", team_id="shared-team"), downstream_called=[])
        statuses.append(cap["status"])
    assert statuses[:4] == [200, 200, 200, 200]
    assert statuses[4] == 429
    cap = await _drive(_auth(user_id="ux", team_id="shared-team"), downstream_called=[])
    body = json.loads(cap["body"])
    assert body["error"]["scope"] == "TEAM"
