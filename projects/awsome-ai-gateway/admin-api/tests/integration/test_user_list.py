# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GET /admin/users integration — email filter param wiring (router → service)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from httpx import AsyncClient


class TestListUsersEmailFilter:
    async def test_email_query_param_passed_to_service(
        self, client: AsyncClient, admin_headers: dict
    ):
        """?email=... 가 UserTeamService.list_users 로 전달되어야 한다."""
        with patch(
            "app.services.user_team_service.UserRepository"
        ) as MockRepo:
            mock_list = AsyncMock(return_value=[])
            MockRepo.return_value.list_users = mock_list

            resp = await client.get(
                "/admin/users?email=Foo@Bar.com", headers=admin_headers
            )

        assert resp.status_code == 200
        assert resp.json()["items"] == []
        assert mock_list.await_args.kwargs["email"] == "Foo@Bar.com"

    async def test_no_email_param_passes_none(
        self, client: AsyncClient, admin_headers: dict
    ):
        """email 미지정 시 None 전달 — 기존 동작 하위호환."""
        with patch(
            "app.services.user_team_service.UserRepository"
        ) as MockRepo:
            mock_list = AsyncMock(return_value=[])
            MockRepo.return_value.list_users = mock_list

            resp = await client.get("/admin/users", headers=admin_headers)

        assert resp.status_code == 200
        assert mock_list.await_args.kwargs["email"] is None
