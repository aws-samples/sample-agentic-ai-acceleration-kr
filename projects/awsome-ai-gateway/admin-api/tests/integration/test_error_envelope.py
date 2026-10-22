# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""모든 에러 응답이 같은 봉투/미디어타입으로 나가는지 — 422 와 미처리 예외.

배경(FE↔BE 정합성 감사):
  * FastAPI 기본 422 는 `{"detail": [{loc,msg,type}, ...]}` **배열**이다. admin-ui 의
    api-client 는 detail 을 문자열로 가정해 그대로 토스트 메시지에 넣었고, 화면에는
    "[object Object]" 만 떴다. 어느 필드가 틀렸는지 사용자가 알 수 없었다.
  * 미처리 예외에는 핸들러가 아예 없어서 Starlette 기본 응답인
    `Internal Server Error`(**text/plain**)이 나갔다. JSON 을 기대하는 클라이언트는
    파싱에 실패하고 error_code·request_id 를 모두 잃었다.

이 테스트는 tests/integration/conftest.py 의 테스트 앱을 쓴다. 그 앱은 이제
app.main.register_exception_handlers 를 **공유**하므로(예전엔 핸들러를 손으로
복제해 두 쪽이 갈라졌다), 여기서 통과하면 프로덕션 앱도 같은 봉투를 낸다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from app.core.db import SESSION_STATE_ATTR, CommittingRoute, get_db_session


@pytest.mark.asyncio
async def test_422_is_normalized_to_the_error_envelope(client: AsyncClient, admin_headers):
    """group_by 에 미지원 값 → 422 + {"error": {...}} + 사람이 읽는 message."""
    res = await client.get(
        "/admin/analytics",
        params={"period": "2026-09", "group_by": "department"},
        headers=admin_headers,
    )
    assert res.status_code == 422, res.text
    assert res.headers["content-type"].startswith("application/json")
    body = res.json()

    # 봉투가 다른 에러들과 동일해야 한다 — api-client 가 error.code/error.message 를 읽는다.
    assert "error" in body, f"봉투 없음(FastAPI 기본 detail 배열 그대로?): {body}"
    err = body["error"]
    assert err["type"] == "validation_error"
    assert err["code"] == "REQUEST_VALIDATION_ERROR"

    # message 는 사람이 읽을 수 있는 'field: reason' 문자열이어야 한다.
    msg = err["message"]
    assert isinstance(msg, str) and msg, f"message 가 문자열이 아님: {msg!r}"
    assert "object Object" not in msg
    assert "group_by" in msg, f"어느 필드가 틀렸는지 알 수 없다: {msg!r}"

    # 원본 배열은 fields 로 보존(폼 단위 인라인 에러 표시에 쓸 수 있게).
    assert isinstance(err["fields"], list) and err["fields"]
    assert "group_by" in [str(p) for p in err["fields"][0]["loc"]]


@pytest.mark.asyncio
async def test_unhandled_exception_returns_json_not_text_plain(test_app: FastAPI):
    """최후의 그물: 미처리 예외도 JSON 봉투 + request_id 로 나가야 한다."""

    @test_app.get("/__boom")
    async def boom():
        raise RuntimeError("db exploded with password hunter2")

    # raise_app_exceptions=False — ServerErrorMiddleware 는 응답을 보낸 뒤 예외를
    # 다시 던진다(운영에서는 로깅/트레이싱용). 여기서는 응답 본문을 검사한다.
    transport = ASGITransport(app=test_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/__boom", headers={"x-request-id": "req-abc-123"})

    assert res.status_code == 500
    assert res.headers["content-type"].startswith("application/json"), (
        f"text/plain 로 새고 있다(Starlette 기본): {res.headers.get('content-type')}"
    )
    err = res.json()["error"]
    assert err["type"] == "internal_error"
    assert err["code"] == "INTERNAL_ERROR"
    assert err["request_id"] == "req-abc-123"
    # 예외 문자열을 본문에 담지 않는다 — 내부 구조/자격증명 노출 방지.
    assert "hunter2" not in res.text and "exploded" not in res.text


def test_test_app_has_the_same_commit_wiring_as_production(test_app: FastAPI):
    """테스트 앱의 모든 라우트도 CommittingRoute 여야 한다.

    안 그러면 통합테스트는 프로덕션과 **다른 앱 형상**을 검증한다: 커밋이 응답 뒤에
    일어나는 예전 동작으로 통과해 버리고, 아래 커밋 실패 테스트도 조용히 무의미해진다.
    (핸들러를 손으로 복제해 두 쪽이 갈라졌던 것과 정확히 같은 부류의 틈이다.)
    """
    stale = [
        f"{sorted(r.methods)} {r.path}"
        for r in test_app.routes
        if isinstance(r, APIRoute) and not isinstance(r, CommittingRoute)
    ]
    assert not stale, (
        f"승격되지 않은 라우트 {len(stale)}개 — conftest 의 "
        f"install_commit_before_response 호출이 빠졌거나 include_router 앞에 있다. "
        f"예: {stale[:5]}"
    )
    total = sum(1 for r in test_app.routes if isinstance(r, APIRoute))
    assert total > 20, f"APIRoute 를 {total}개만 찾았다 — 검사 범위가 잘못됐다"


@pytest.mark.asyncio
async def test_real_router_write_reports_a_failed_commit_as_500(test_app: FastAPI):
    """실제 라우터(POST /admin/models)에서 커밋이 실패하면 201 이 아니라 500 이어야 한다.

    tests/regression 쪽은 최소 앱으로 CommittingRoute 자체를 증명한다. 여기서는 실제
    라우터·실제 스키마·실제 봉투로 같은 계약을 확인한다 — 라우트 승격은 됐는데
    request.state 배선이 끊어져 커밋 경로를 밟지 못하는 경우를 잡기 위해서다.
    """
    committed: dict[str, bool] = {"attempted": False}

    async def failing_session(request: Request):
        session = AsyncMock()
        # 결과 객체는 동기 API 다(.scalars().all()) — 형제 계정 조회(model_service)가 빈 결과를 본다.
        session.execute = AsyncMock(return_value=MagicMock())
        session.get = AsyncMock(return_value=None)
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.in_transaction = MagicMock(return_value=True)

        async def _boom():
            committed["attempted"] = True
            raise RuntimeError("could not serialize access due to concurrent update")

        session.commit = AsyncMock(side_effect=_boom)
        session.rollback = AsyncMock()
        setattr(request.state, SESSION_STATE_ATTR, session)
        yield session

    test_app.dependency_overrides[get_db_session] = failing_session

    def _dates(model):
        model.created_at = datetime.now(timezone.utc)
        model.updated_at = datetime.now(timezone.utc)
        return model

    with patch("app.services.model_service.ModelRepository") as MockRepo, \
         patch("app.services.model_service.audit_logger") as mock_audit:
        repo = MockRepo.return_value
        repo.alias_exists_ci = AsyncMock(return_value=False)
        repo.create_model = AsyncMock(side_effect=_dates)
        repo.create_pricing = AsyncMock()
        mock_audit.log = AsyncMock()

        transport = ASGITransport(app=test_app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.post(
                "/admin/models",
                json={
                    "alias": "claude-sonnet-commitfail",
                    "provider": "BEDROCK",
                    "provider_model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "api_format": "BEDROCK_NATIVE",
                    "input_price_per_1k_tokens": "0.003",
                    "output_price_per_1k_tokens": "0.015",
                },
                headers={"Authorization": "Bearer admin-token"},
            )

    assert committed["attempted"], (
        "커밋이 시도조차 되지 않았다 — request.state 배선이 끊어져 CommittingRoute 가 "
        "세션을 찾지 못한다(이 테스트가 공허해진다)"
    )
    assert res.status_code == 500, (
        f"커밋 실패인데 {res.status_code} — 사용자는 모델이 등록됐다고 믿는다: {res.text[:300]}"
    )
    assert res.headers["content-type"].startswith("application/json")
    err = res.json()["error"]
    assert err["type"] == "internal_error"
    assert err["code"] == "INTERNAL_ERROR"
    assert "alias" not in res.text, "성공 본문이 새고 있다"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/admin/keys", "/admin/users", "/admin/monitoring/events"],
)
@pytest.mark.parametrize("bad_limit", ["0", "-1", "1000000"])
async def test_pagination_limit_is_bounded(
    client: AsyncClient, admin_headers, path: str, bad_limit: str
):
    """커서 엔드포인트의 limit 은 [1, 200] 밖이면 422.

    예전엔 /admin/keys·/admin/users 가 `limit: int = 50` 로 아무 경계가 없어서
    ?limit=1000000 이 한 요청으로 테이블 전체를 끌어왔고, ?limit=0/-1 은
    PostgreSQL 이 "LIMIT must not be negative" 로 거부해 정체불명의 500 이 됐다.
    /admin/monitoring/events 는 상한만 있고 하한이 없었다.
    """
    res = await client.get(path, params={"limit": bad_limit}, headers=admin_headers)
    assert res.status_code == 422, f"{path}?limit={bad_limit} → {res.status_code}"
    assert "limit" in res.json()["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin/keys", "/admin/users", "/admin/monitoring/events"])
async def test_pagination_limit_boundaries_are_accepted(
    client: AsyncClient, admin_headers, path: str
):
    """경계값 1 과 200 은 통과해야 한다(off-by-one 으로 좁히지 않았는지)."""
    for ok_limit in ("1", "200"):
        res = await client.get(path, params={"limit": ok_limit}, headers=admin_headers)
        assert res.status_code == 200, f"{path}?limit={ok_limit} → {res.status_code} {res.text[:200]}"
