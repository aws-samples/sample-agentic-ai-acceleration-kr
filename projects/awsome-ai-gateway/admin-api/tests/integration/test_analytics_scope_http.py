# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""HTTP 경계: GET /admin/analytics 가 닫힌 scope 집합만 받는다.

  * pattern(`^(all|team:<36자>)$`)을 벗어난 값 → FastAPI 검증 실패 **422** (group_by Literal 과 같은 봉투).
  * pattern 은 통과하지만 UUID 가 아닌 값(`team:` + 대시 36개) → 서비스가 닫아 **400**.
  * DEVELOPER 는 scope 와 무관하게 403(권한 의존성이 먼저 돈다).
예전엔 TEAM_LEADER 의 `scope=xyz` 가 200 + 전사 데이터였고, `team:not-a-uuid` 는 500 이었다.
"""
from __future__ import annotations

from urllib.parse import quote

import pytest
from httpx import AsyncClient

ROUTES = ["/admin/analytics"]
PATTERN_REJECTS = ["xyz", "ALL", " all", "team:garbage", "team:not-a-uuid", "all%"]


def _url(path: str, scope: str) -> str:
    return f"{path}?period=2026-09&group_by=user&scope={quote(scope, safe='')}"


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("scope", PATTERN_REJECTS)
async def test_leader_malformed_scope_is_rejected(client: AsyncClient, leader_headers: dict, path, scope):
    resp = await client.get(_url(path, scope), headers=leader_headers)
    assert resp.status_code == 422, (scope, resp.status_code, resp.text[:200])
    assert "scope" in resp.text


@pytest.mark.parametrize("path", ROUTES)
async def test_pattern_passing_non_uuid_is_a_400(client: AsyncClient, leader_headers: dict, path):
    resp = await client.get(_url(path, "team:" + "-" * 36), headers=leader_headers)
    assert resp.status_code == 400, resp.text[:200]


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("scope", ["all", "xyz", "team:garbage"])
async def test_developer_is_forbidden_whatever_the_scope(client: AsyncClient, dev_headers: dict, path, scope):
    resp = await client.get(_url(path, scope), headers=dev_headers)
    assert resp.status_code == 403, (scope, resp.status_code, resp.text[:200])


@pytest.mark.parametrize("path", ROUTES)
async def test_leader_all_is_still_served(client: AsyncClient, leader_headers: dict, path):
    """대조군 — 정상 scope 는 그대로 200(서비스가 자기 팀으로 좁힌다)."""
    resp = await client.get(_url(path, "all"), headers=leader_headers)
    assert resp.status_code == 200, resp.text[:200]
