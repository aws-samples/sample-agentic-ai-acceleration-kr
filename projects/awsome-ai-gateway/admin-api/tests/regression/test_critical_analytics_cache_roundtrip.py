# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Regression: /admin/analytics 캐시 적중 응답이 미스 응답과 같은 JSON 객체여야 한다.

증상(dev, admin-ui 분석 화면): 몇 번 누르면 "An error occurred in the Server Components
render" 로 페이지 전체가 깨진다. 서버 로그는
``TypeError: Cannot read properties of undefined (reading 'total_cost_usd')``
(ROIMetricsCards 의 ``data.cost_summary.total_cost_usd``).

원인: ``_cache_set`` 이 Pydantic 모델(AnalyticsResponse)을 ``json.dumps(value, default=str)``
로 저장했다. 모델은 JSON 직렬화가 안 되니 ``default=str`` 가 모델의 ``str()``
(``"period='2026-09' currency='USD' cost_summary=CostSummary(...)"``)을 **문자열 하나**로
넣고, 적중 시 ``json.loads`` 가 그 문자열을 돌려줘 응답 본문이 객체가 아니라 문자열이 됐다.
첫 요청(미스)만 정상이고 TTL(30초) 동안 같은 요청은 전부 문자열 — "몇 번 누르면" 의 정체다.

기존 role 격리 테스트(test_critical_analytics_cache_role_isolation)는 서비스 대역이 **dict**
를 돌려줘서 이 왕복을 한 번도 타지 않았다. 여기서는 실제 응답 모델과, 실제 Redis 처럼
**문자열을 저장하는** 대역으로 HTTP 경로 그대로 두 번 부른다.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_admin_or_team_leader
from app.core.db import get_db_session
from app.models.auth import UserRole
from app.routers import analytics as an
from app.schemas.analytics import AnalyticsResponse, CostSummary, ModelBreakdown, TrendItem

QUERY = {"period": "2026-09", "group_by": "model", "scope": "all"}


class _StrRedis:
    """실제 Redis 처럼 값을 **문자열로** 저장하는 대역(get/setex 만)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        assert isinstance(value, str), "Redis 에는 문자열만 들어간다"
        self.store[key] = value


class _Svc:
    """운영과 같은 타입(AnalyticsResponse)을 돌려주는 서비스 대역."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_analytics(
        self, session, *, period, group_by, scope, actor, start_date=None, end_date=None
    ):
        self.calls += 1
        return AnalyticsResponse(
            period=period,
            cost_summary=CostSummary(
                total_requests=153,
                total_tokens=1_304_543,
                total_cost_usd=Decimal("0.624388"),
                active_users=2,
                avg_cost_per_user_usd=Decimal("0.312194"),
            ),
            by_model=[ModelBreakdown(model="claude-haiku-5-5", requests=120, cost_usd=Decimal("0.51"))],
            trends=[TrendItem(date="2026-09-14", cost_usd=Decimal("0.6"), requests=153)],
        )


def _admin():
    return type("U", (), {"role": UserRole.ADMIN, "team_id": None, "user_id": uuid.uuid4()})()


def _client(redis, svc) -> TestClient:
    app = FastAPI()
    app.include_router(an.router)
    app.state.redis = redis
    app.state.analytics_service = svc
    app.dependency_overrides[require_admin_or_team_leader] = _admin
    app.dependency_overrides[get_db_session] = lambda: None
    return TestClient(app)


def _key() -> str:
    return an._analytics_cache_key(period=QUERY["period"], group_by=QUERY["group_by"])


# ──────────────────────────────────────────────────────────────────────────────
# 0) 대조군 — 예전 저장 방식이 정말 문자열을 만든다(아래 오염 항목 시드가 공허하지 않음)
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_old_serialisation_really_turns_the_model_into_a_string():
    model = await _Svc().get_analytics(None, period="2026-09", group_by="model", scope="all", actor=_admin())
    decoded = json.loads(json.dumps(model, default=str))
    assert isinstance(decoded, str) and "cost_summary=CostSummary(" in decoded


# ──────────────────────────────────────────────────────────────────────────────
# 1) 핵심 — 두 번째(적중) 응답이 첫 번째(미스) 응답과 같은 객체
# ──────────────────────────────────────────────────────────────────────────────
def test_cache_hit_returns_the_same_json_object_as_the_miss():
    redis, svc = _StrRedis(), _Svc()
    client = _client(redis, svc)

    miss = client.get("/admin/analytics", params=QUERY)
    hit = client.get("/admin/analytics", params=QUERY)

    assert miss.status_code == hit.status_code == 200
    assert svc.calls == 1, "두 번째 요청은 캐시에서 나와야 한다 — 아니면 이 테스트가 공허하다"
    assert isinstance(hit.json(), dict), f"적중 응답이 객체가 아니다: {hit.text[:120]}"
    assert hit.json() == miss.json()
    assert hit.json()["cost_summary"]["total_cost_usd"] == "0.624388"


def test_the_cached_value_is_a_json_object():
    redis, svc = _StrRedis(), _Svc()
    _client(redis, svc).get("/admin/analytics", params=QUERY)
    assert isinstance(json.loads(redis.store[_key()]), dict)


# ──────────────────────────────────────────────────────────────────────────────
# 2) 롤링 배포 — 수정 전 파드가 쓴 문자열 항목은 miss 로 보고 다시 채운다
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_string_entry_left_by_an_old_pod_is_treated_as_a_miss():
    redis, svc = _StrRedis(), _Svc()
    old = await _Svc().get_analytics(None, period="2026-09", group_by="model", scope="all", actor=_admin())
    redis.store[_key()] = json.dumps(old, default=str)  # 수정 전 _cache_set 이 쓰던 그대로

    resp = _client(redis, svc).get("/admin/analytics", params=QUERY)

    assert svc.calls == 1, "오염 항목을 적중으로 쓰면 안 된다"
    assert isinstance(resp.json(), dict) and "cost_summary" in resp.json()
    assert isinstance(json.loads(redis.store[_key()]), dict), "다시 채운 항목은 객체여야 한다"
