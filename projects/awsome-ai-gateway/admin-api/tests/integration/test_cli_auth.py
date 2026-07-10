# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""CLI auth integration tests — STS verification + VK issuance."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from app.models.auth import User, UserRole


class TestCLIVirtualKeyIssuance:
    async def test_cli_virtual_key_ssrf_rejected(self, client: AsyncClient):
        resp = await client.post(
            "/cli/auth/virtual-key",
            json={
                "sts_request": {
                    "url": "https://evil.com/?Action=GetCallerIdentity",
                    "headers": {},
                },
                "device_name": "test",
            },
        )
        assert resp.status_code == 401
        assert "host not allowed" in resp.json()["error"]["message"]

    async def test_cli_virtual_key_wrong_action_rejected(self, client: AsyncClient):
        resp = await client.post(
            "/cli/auth/virtual-key",
            json={
                "sts_request": {
                    "url": "https://sts.ap-northeast-2.amazonaws.com/?Action=AssumeRole",
                    "headers": {},
                },
                "device_name": "test",
            },
        )
        assert resp.status_code == 401
        assert "GetCallerIdentity" in resp.json()["error"]["message"]

    async def test_cli_virtual_key_success(self, client: AsyncClient):
        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::123456789:assumed-role/AllowedRole/user@corp.com</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        default_team_id = str(uuid.uuid4())

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx, \
             patch("app.services.cli_service.UserRepository") as MockUserRepo, \
             patch("app.services.cli_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.key_service.KeyRepository") as MockKeyRepo, \
             patch("app.services.key_service.audit_logger") as mock_audit:

            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]
            settings.DEFAULT_TEAM_ID = default_team_id
            settings.DEFAULT_USER_BUDGET_USD = 100.0

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            user = MagicMock(spec=User)
            user.id = uuid.uuid4()
            user.email = "user@corp.com"
            user.role = UserRole.DEVELOPER
            user.team_id = uuid.UUID(default_team_id)
            user.is_active = True
            MockUserRepo.return_value.get_by_sso_subject = AsyncMock(return_value=user)
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=None)

            key_repo = MockKeyRepo.return_value
            key_repo.expire_active_keys = AsyncMock(return_value=0)
            key_repo.create = AsyncMock(side_effect=lambda vk: vk)
            mock_audit.log = AsyncMock()

            resp = await client.post(
                "/cli/auth/virtual-key",
                json={
                    "sts_request": {
                        "url": "https://sts.ap-northeast-2.amazonaws.com/?Action=GetCallerIdentity",
                        "headers": {"Authorization": "AWS4-HMAC-SHA256 ..."},
                    },
                    "device_name": "macbook",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["virtual_key"].startswith("vk-")
        assert data["user_id"] == str(user.id)


class TestCLISetup:
    async def test_setup_returns_tool_configs(self, client: AsyncClient):
        resp = await client.post(
            "/cli/setup",
            json={
                "device_name": "test",
                "os": "darwin",
                "arch": "arm64",
                "detected_tools": ["claude-code", "cursor"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "claude-code" in data["tool_configs"]
        assert data["tool_configs"]["claude-code"]["use_api_key_helper"] is True
