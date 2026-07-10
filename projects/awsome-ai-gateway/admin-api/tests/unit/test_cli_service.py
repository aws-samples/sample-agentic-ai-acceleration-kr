# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import ForbiddenError, STSVerificationError
from app.models.auth import User, UserRole
from app.schemas.cli import STSRequestPayload, VirtualKeyIssueRequest
from app.services.cli_service import CLIService
from app.services.key_service import KeyService


@pytest.fixture
def mock_key_service() -> AsyncMock:
    svc = AsyncMock(spec=KeyService)
    svc.issue_key = AsyncMock(return_value=MagicMock(
        virtual_key="vk-test123",
        expires_at=MagicMock(),
    ))
    return svc


@pytest.fixture
def cli_service(mock_key_service: AsyncMock) -> CLIService:
    return CLIService(key_service=mock_key_service)


class TestSSRFPrevention:
    async def test_rejects_non_sts_host(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://evil.com/?Action=GetCallerIdentity",
                headers={},
            ),
            device_name="test",
        )

        with patch("app.services.cli_service.get_settings") as mock_settings:
            mock_settings.return_value.ALLOWED_STS_REGIONS = ["ap-northeast-2"]

            with pytest.raises(STSVerificationError, match="host not allowed"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)

    async def test_rejects_non_getcalleridentity_action(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.ap-northeast-2.amazonaws.com/?Action=AssumeRole",
                headers={},
            ),
            device_name="test",
        )

        with patch("app.services.cli_service.get_settings") as mock_settings:
            mock_settings.return_value.ALLOWED_STS_REGIONS = ["ap-northeast-2"]

            with pytest.raises(STSVerificationError, match="GetCallerIdentity"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)

    async def test_accepts_valid_sts_global_endpoint(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.amazonaws.com/?Action=GetCallerIdentity",
                headers={"Authorization": "AWS4-HMAC-SHA256 ..."},
            ),
            device_name="test",
        )

        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::123456789:assumed-role/AllowedRole/user@corp.com</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]
            settings.DEFAULT_TEAM_ID = str(uuid.uuid4())
            settings.DEFAULT_USER_BUDGET_USD = 100.0

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with patch("app.services.cli_service.UserRepository") as MockUserRepo, \
                 patch("app.services.cli_service.BudgetRepository") as MockBudgetRepo:
                user = MagicMock(spec=User)
                user.id = uuid.uuid4()
                user.email = "user@corp.com"
                user.role = UserRole.DEVELOPER
                user.team_id = uuid.UUID(settings.DEFAULT_TEAM_ID)
                user.is_active = True

                MockUserRepo.return_value.get_by_sso_subject = AsyncMock(return_value=user)
                MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=None)

                result = await cli_service.verify_sts_and_issue_key(mock_session, data=data)

        assert result.user_id == str(user.id)


class TestIAMRoleCheck:
    async def test_rejects_disallowed_iam_role(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.ap-northeast-2.amazonaws.com/?Action=GetCallerIdentity",
                headers={},
            ),
            device_name="test",
        )

        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::123456789:assumed-role/ForbiddenRole/user</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
            settings = mock_settings.return_value
            settings.ALLOWED_STS_REGIONS = ["ap-northeast-2"]
            settings.ALLOWED_IAM_ROLES = ["AllowedRole"]

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = sts_xml

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_httpx.AsyncClient.return_value = mock_client

            with pytest.raises(ForbiddenError, match="ForbiddenRole"):
                await cli_service.verify_sts_and_issue_key(mock_session, data=data)


class TestAutoProvisioning:
    async def test_auto_provisions_new_user(self, cli_service: CLIService, mock_session: AsyncMock):
        data = VirtualKeyIssueRequest(
            sts_request=STSRequestPayload(
                url="https://sts.ap-northeast-2.amazonaws.com/?Action=GetCallerIdentity",
                headers={},
            ),
            device_name="test",
        )

        sts_xml = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
            <GetCallerIdentityResult>
                <Arn>arn:aws:sts::123456789:assumed-role/AllowedRole/newuser@corp.com</Arn>
            </GetCallerIdentityResult>
        </GetCallerIdentityResponse>"""

        default_team_id = str(uuid.uuid4())

        with patch("app.services.cli_service.get_settings") as mock_settings, \
             patch("app.services.cli_service.httpx") as mock_httpx:
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

            with patch("app.services.cli_service.UserRepository") as MockUserRepo, \
                 patch("app.services.cli_service.BudgetRepository") as MockBudgetRepo:
                # User not found → auto-provision
                MockUserRepo.return_value.get_by_sso_subject = AsyncMock(return_value=None)

                new_user = MagicMock(spec=User)
                new_user.id = uuid.uuid4()
                new_user.email = "newuser@corp.com"
                new_user.role = UserRole.DEVELOPER
                new_user.team_id = uuid.UUID(default_team_id)
                new_user.is_active = True
                MockUserRepo.return_value.create_user = AsyncMock(return_value=new_user)

                MockBudgetRepo.return_value.upsert_config = AsyncMock()
                MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=None)

                result = await cli_service.verify_sts_and_issue_key(mock_session, data=data)

        MockUserRepo.return_value.create_user.assert_called_once()
        assert result.user_id == str(new_user.id)
