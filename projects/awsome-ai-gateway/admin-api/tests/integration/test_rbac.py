# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""RBAC integration tests — verify role-based access control across all routers."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


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

    async def test_leader_can_access_budget_summary(self, client: AsyncClient, leader_headers: dict):
        # This will fail at the service layer (no DB), but should not fail at auth
        resp = await client.get(
            "/admin/budgets/summary?period=2026-04",
            headers=leader_headers,
        )
        # 500 is expected (no DB), but not 403
        assert resp.status_code != 403

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

    async def test_cli_download_no_auth_required(self, client: AsyncClient):
        resp = await client.get("/cli/download/darwin/arm64")
        assert resp.status_code == 200


class TestHealthEndpoint:
    async def test_health_no_auth_required(self, client: AsyncClient):
        resp = await client.get("/health")
        # May be 503 (degraded) without real DB/Redis, but should not be 401/403
        assert resp.status_code in (200, 503)
