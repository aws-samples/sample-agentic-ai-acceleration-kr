# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""RBAC integration tests — verify role-based access control across all routers."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from tests.integration.conftest import ADMIN_TEAM_ID


class TestAdminOnlyEndpoints:
    """Endpoints that require ADMIN role."""

    ADMIN_ONLY_ENDPOINTS = [
        ("GET", "/admin/keys"),
        ("GET", "/admin/models"),
        ("GET", "/admin/users"),
    ]

    @pytest.mark.parametrize("method,url", ADMIN_ONLY_ENDPOINTS)
    async def test_developer_rejected(self, client: AsyncClient, dev_headers: dict, method: str, url: str):
        resp = await client.request(method, url, headers=dev_headers)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,url", ADMIN_ONLY_ENDPOINTS)
    async def test_no_auth_rejected(self, client: AsyncClient, method: str, url: str):
        resp = await client.request(method, url)
        assert resp.status_code == 401

    async def test_invalid_token_rejected(self, client: AsyncClient):
        resp = await client.get("/admin/keys", headers={"Authorization": "Bearer invalid-token"})
        assert resp.status_code == 401


class TestAdminOrTeamLeaderEndpoints:
    """Endpoints that allow both ADMIN and TEAM_LEADER."""

    # 리더 토큰의 팀(…a1) 멤버 1명 · 다른 팀 멤버 1명 · 팀 없는 사용자 1명, 그리고 두 팀.
    _OTHER_TEAM_ID = uuid.UUID("20000000-0000-4000-8000-000000000002")
    _USERS = [
        SimpleNamespace(id=uuid.UUID("10000000-0000-4000-8000-000000000001"), team_id=ADMIN_TEAM_ID, display_name="Own Member",
                        email="own@example.com", is_active=True),
        SimpleNamespace(id=uuid.UUID("10000000-0000-4000-8000-000000000002"), team_id=_OTHER_TEAM_ID, display_name="Other Member",
                        email="other@example.com", is_active=True),
        SimpleNamespace(id=uuid.UUID("10000000-0000-4000-8000-000000000003"), team_id=None, display_name="No Team",
                        email="noteam@example.com", is_active=True),
    ]
    _TEAMS = [
        SimpleNamespace(id=ADMIN_TEAM_ID, name="Own Team", members=[], department=None),
        SimpleNamespace(id=_OTHER_TEAM_ID, name="Other Team", members=[], department=None),
    ]

    async def _summary(self, client: AsyncClient, headers: dict) -> set[str]:
        with patch("app.repositories.user_repository.UserRepository.iter_all_users",
                   AsyncMock(return_value=self._USERS)), \
             patch("app.repositories.user_repository.UserRepository.list_all_teams",
                   AsyncMock(return_value=self._TEAMS)), \
             patch("app.services.budget_service.BudgetRepository.list_configs", AsyncMock(return_value=[])):
            resp = await client.get("/admin/budgets/summary?period=2026-04", headers=headers)
        assert resp.status_code == 200, resp.text
        return {item["target_id"] for item in resp.json()["summary"]}

    async def test_leader_budget_summary_is_scoped_to_the_own_team(self, client: AsyncClient, leader_headers: dict):
        """리더는 자기 팀과 그 멤버만 받는다 — 예전엔 '403 이 아니다' 만 봤고, 실제로는 전사 요약이 나갔다."""
        ids = await self._summary(client, leader_headers)
        assert ids == {str(ADMIN_TEAM_ID), str(self._USERS[0].id)}, ids

    async def test_admin_budget_summary_is_org_wide(self, client: AsyncClient, admin_headers: dict):
        """대조군 — 관리자는 모든 팀과 팀 없는 사용자까지 받는다."""
        ids = await self._summary(client, admin_headers)
        assert ids == {str(t.id) for t in self._TEAMS} | {str(u.id) for u in self._USERS}

    async def test_leader_cannot_read_another_teams_allocation(self, client: AsyncClient, leader_headers: dict):
        resp = await client.get(f"/admin/budgets/team/{self._OTHER_TEAM_ID}/allocation", headers=leader_headers)
        assert resp.status_code == 403, resp.text

    async def test_leader_can_access_analytics(self, client: AsyncClient, leader_headers: dict):
        resp = await client.get(
            "/admin/analytics?period=2026-04",
            headers=leader_headers,
        )
        assert resp.status_code != 403

    async def test_developer_rejected_from_budget_summary(self, client: AsyncClient, dev_headers: dict):
        resp = await client.get(
            "/admin/budgets/summary?period=2026-04",
            headers=dev_headers,
        )
        assert resp.status_code == 403

    async def test_developer_rejected_from_analytics(self, client: AsyncClient, dev_headers: dict):
        resp = await client.get(
            "/admin/analytics?period=2026-04",
            headers=dev_headers,
        )
        assert resp.status_code == 403


class TestCLIEndpointsNoAuth:
    """CLI endpoints do not require JWT auth."""

    async def test_cli_setup_no_auth_required(self, client: AsyncClient):
        resp = await client.post("/cli/setup", json={
            "device_name": "test-device",
            "os": "darwin",
            "arch": "arm64",
            "detected_tools": ["claude-code"],
        })
        assert resp.status_code == 200

class TestHealthEndpoint:
    async def test_health_no_auth_required(self, client: AsyncClient):
        resp = await client.get("/health")
        # May be 503 (degraded) without real DB/Redis, but should not be 401/403
        assert resp.status_code in (200, 503)
