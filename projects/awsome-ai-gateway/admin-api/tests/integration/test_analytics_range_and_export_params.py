# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""라우트 레벨: 날짜 범위 파라미터가 **선언되어** 실제로 도달하는지.

서비스 유닛 테스트로는 이 결함을 잡을 수 없다 — 결함은 "라우트가 파라미터를 선언하지
않아 FastAPI 가 조용히 버린다" 는 데 있고, 서비스는 인자를 받으면 잘 동작했다.
그래서 반드시 HTTP 레이어를 통과시켜 확인한다(DB 는 conftest 의 빈 결과 mock).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

async def test_analytics_honors_start_and_end_date(client: AsyncClient, admin_headers: dict):
    resp = await client.get(
        "/admin/analytics?period=2026-09&group_by=model&scope=all"
        "&start_date=2026-09-01&end_date=2026-09-05",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 선언되지 않은 파라미터는 FastAPI 가 422 없이 버린다 → 이 두 필드가 비어 오면
    # 서버는 5일 범위 요청을 한 달치로 답한 것이다(예전 동작).
    assert (body["start_date"], body["end_date"]) == ("2026-09-01", "2026-09-05")


async def test_analytics_month_request_reports_no_range(client: AsyncClient, admin_headers: dict):
    """대조군 — 월 요청은 범위 필드가 null 이다(범위를 지어내지 않는다)."""
    resp = await client.get("/admin/analytics?period=2026-09", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_date"] is None and body["end_date"] is None


@pytest.mark.parametrize(
    "qs",
    [
        "&start_date=2026-09-05",  # end 누락
        "&end_date=2026-09-05",  # start 누락
        "&start_date=2026-09-09&end_date=2026-09-01",  # 역순
        "&start_date=2026-09-31&end_date=2026-10-01",  # 달력에 없는 날짜
    ],
)
async def test_unhonorable_range_fails_loudly_over_http(
    client: AsyncClient, admin_headers: dict, qs: str
):
    resp = await client.get(f"/admin/analytics?period=2026-09{qs}", headers=admin_headers)
    assert resp.status_code == 400, resp.text


async def test_cache_key_includes_the_date_range(
    client: AsyncClient, admin_headers: dict, test_app
):
    """30초 캐시가 범위를 키에 안 넣으면, 범위를 바꿔도 앞 범위의 숫자가 그대로 나간다."""
    redis = test_app.state.redis

    redis.setex.reset_mock()
    await client.get(
        "/admin/analytics?period=2026-09&start_date=2026-09-01&end_date=2026-09-05",
        headers=admin_headers,
    )
    first_key = redis.setex.call_args.args[0]

    redis.setex.reset_mock()
    await client.get(
        "/admin/analytics?period=2026-09&start_date=2026-09-06&end_date=2026-09-10",
        headers=admin_headers,
    )
    second_key = redis.setex.call_args.args[0]

    redis.setex.reset_mock()
    await client.get("/admin/analytics?period=2026-09", headers=admin_headers)
    month_key = redis.setex.call_args.args[0]

    assert first_key != second_key, "다른 범위가 같은 캐시 키를 쓴다"
    assert "2026-09-01" in first_key and "2026-09-05" in first_key
    assert month_key == "analytics:global:2026-09:model", "월 키 모양이 바뀌었다"
    assert month_key not in (first_key, second_key)


