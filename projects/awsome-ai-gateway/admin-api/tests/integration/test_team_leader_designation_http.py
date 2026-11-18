# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``PUT /admin/teams/{team_id}/leader`` 의 거부가 프로젝트 에러 봉투로 나간다.

규칙은 tests/regression/test_high_team_leader_role_survives_signin.py 가 고정한다. 여기서는 라우터와 예외
핸들러를 거친 상태 코드·본문만 본다 — 호출자는 ``error.message`` 로 거부 이유를 안다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.models.auth import Team, User, UserRole

TEAM_ID = uuid.uuid4()
OTHER_TEAM_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


def _team() -> Team:
    return Team(id=TEAM_ID, dept_id=uuid.uuid4(), name="A", leader_user_id=None, created_at=datetime.now(timezone.utc))


def _user(*, team_id: uuid.UUID, is_active: bool = True) -> User:
    return User(
        id=USER_ID,
        email="kim@example.com",
        display_name="kim",
        role=UserRole.DEVELOPER,
        sso_subject="sub-kim",
        team_id=team_id,
        is_active=is_active,
        provider="cognito",
    )


@pytest.mark.parametrize(
    ("user", "status", "error"),
    [
        pytest.param(
            _user(team_id=OTHER_TEAM_ID),
            400,
            {
                "type": "validation_error",
                "code": "validation_error",
                "message": f"User {USER_ID} is not a member of team {TEAM_ID}",
            },
            id="member of another team",
        ),
        pytest.param(
            _user(team_id=TEAM_ID, is_active=False),
            400,
            {"type": "validation_error", "code": "validation_error", "message": f"User {USER_ID} is deactivated"},
            id="deactivated member",
        ),
        pytest.param(
            None,
            404,
            {"type": "not_found", "code": "not_found", "message": f"User not found: {USER_ID}"},
            id="unknown user",
        ),
    ],
)
async def test_refused_designation_is_an_error_envelope_and_writes_nothing(
    client: AsyncClient, admin_headers: dict, user, status, error
):
    with (
        patch("app.services.user_team_service.UserRepository") as MockRepo,
        patch("app.services.user_team_service.audit_logger") as audit,
    ):
        repo = MockRepo.return_value
        repo.get_team = AsyncMock(return_value=_team())
        repo.get_user = AsyncMock(return_value=user)
        repo.set_leader = AsyncMock()
        repo.update_user_role = AsyncMock()
        audit.log = AsyncMock()

        resp = await client.put(f"/admin/teams/{TEAM_ID}/leader", json={"user_id": str(USER_ID)}, headers=admin_headers)

    assert resp.status_code == status
    assert resp.json() == {"error": error}
    repo.set_leader.assert_not_awaited()
    repo.update_user_role.assert_not_awaited()
    audit.log.assert_not_awaited()
