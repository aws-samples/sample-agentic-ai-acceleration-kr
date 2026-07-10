# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for the /health/ready readiness probe (견고성 6축검증 축⑤).

readiness 는 liveness(/health)와 분리돼야 한다: DB 단독 열화(DB_DEGRADED)나 커넥션 풀
포화 시 503 을 반환해 고장 파드를 endpoint 로테이션에서 제외한다. /health 는 여전히
DB_DEGRADED 에 200 을 주는 관대한 liveness 로 남아 재시작 cascade 를 피한다.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import health as health_router
from app.schemas.domain import DegradationLevel


def _build_app(level: DegradationLevel, *, checked_out=0, pool_size=20, max_overflow=10, engine=True):
    app = FastAPI()
    app.include_router(health_router.router)
    app.state.degradation_manager = SimpleNamespace(level=level)
    if engine:
        pool = MagicMock()
        pool.checkedout.return_value = checked_out
        pool.size.return_value = pool_size
        pool.overflow.return_value = max_overflow
        pool._max_overflow = max_overflow
        app.state.db_engine = SimpleNamespace(pool=pool)
    return app


async def _get(app, path="/health/ready"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


@pytest.mark.asyncio
async def test_ready_when_healthy_and_pool_free():
    resp = await _get(_build_app(DegradationLevel.HEALTHY, checked_out=3))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["pool_saturated"] is False


@pytest.mark.asyncio
async def test_not_ready_when_db_degraded():
    # 핵심 회귀 방지: DB 단독 열화는 /health 에선 200 이지만 readiness 는 503 이어야 한다.
    resp = await _get(_build_app(DegradationLevel.DB_DEGRADED, checked_out=0))
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


@pytest.mark.asyncio
async def test_not_ready_at_hard_cap():
    # 포화 기준 = hard_cap(size+max_overflow=30) 소진(보수적). 모든 슬롯 점유=명백한 고갈.
    resp = await _get(_build_app(DegradationLevel.HEALTHY, checked_out=30, pool_size=20, max_overflow=10))
    assert resp.status_code == 503
    assert resp.json()["pool_saturated"] is True


@pytest.mark.asyncio
async def test_ready_under_transient_load_below_hard_cap():
    # HPA min=1 안전: 상시 풀 초과(25/20+10)여도 hard_cap 미만이면 파드를 빼지 않는다
    # (바쁨≠고장). 진짜 지속 고갈은 DegradationLevel 게이트(HealthChecker)가 잡는다.
    resp = await _get(_build_app(DegradationLevel.HEALTHY, checked_out=25, pool_size=20, max_overflow=10))
    assert resp.status_code == 200
    assert resp.json()["pool_saturated"] is False


@pytest.mark.asyncio
async def test_ready_when_pool_has_room():
    resp = await _get(_build_app(DegradationLevel.HEALTHY, checked_out=19, pool_size=20, max_overflow=10))
    assert resp.status_code == 200
    assert resp.json()["pool_saturated"] is False


@pytest.mark.asyncio
async def test_both_degraded_not_ready():
    resp = await _get(_build_app(DegradationLevel.BOTH_DEGRADED))
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_pool_introspection_failure_is_lenient():
    # pool 조회가 실패해도 readiness 를 떨어뜨리지 않는다(HEALTHY 이면 200).
    app = FastAPI()
    app.include_router(health_router.router)
    app.state.degradation_manager = SimpleNamespace(level=DegradationLevel.HEALTHY)
    bad_pool = MagicMock()
    bad_pool.checkedout.side_effect = RuntimeError("boom")
    app.state.db_engine = SimpleNamespace(pool=bad_pool)
    resp = await _get(app)
    assert resp.status_code == 200
    assert resp.json()["pool"]["error"] == "pool_introspection_failed"


@pytest.mark.asyncio
async def test_health_liveness_still_200_on_db_degraded():
    # /health(liveness)는 DB_DEGRADED 에 여전히 200(관대) — 재시작 cascade 회피 계약 유지.
    resp = await _get(_build_app(DegradationLevel.DB_DEGRADED), path="/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
