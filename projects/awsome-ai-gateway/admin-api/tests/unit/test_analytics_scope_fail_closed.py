# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""analytics 의 scope 는 닫힌 집합('all' | 'team:<uuid>')이다 — 그 밖의 값은 **데이터를 읽기 전에** 400.

결함: get_analytics 는 `startswith("team:")` 와 `== "all"` 두 분기만 있었다. 'xyz'·'ALL'·' all' 은
어느 분기도 타지 않아 roi_scope=None(=GLOBAL)로 떨어졌고, 격리 불변식은 비-GLOBAL 일 때만 검사하므로
TEAM_LEADER 가 전사 비용·다른 팀 사용자 이메일을 받았다. export 는 같은 값을 CSV 로 내려줬다.
'team:<uuid 아님>' 은 uuid.UUID 의 ValueError 가 그대로 올라가 500 이었다.
HTTP 경계(pattern → 422)는 tests/integration/test_analytics_scope_http.py, 실 데이터는
test_high_console_contract_real_db.py 가 증명한다.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError, ValidationError
from app.models.auth import UserRole
from app.services.analytics_service import AnalyticsService

OWN_TEAM = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
BAD_SCOPES = ["xyz", "ALL", " all", "all ", "team:garbage", "team:", "team:" + "-" * 36, "team:a:b", ""]


def _actor(role: UserRole) -> CurrentUser:
    return CurrentUser(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000009"),
        email="x@example.com",
        role=role,
        team_id=OWN_TEAM,
    )


def _session() -> AsyncMock:
    result = MagicMock()
    result.all = MagicMock(return_value=[])
    result.__iter__ = MagicMock(return_value=iter([]))
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


async def _get(scope: str, role: UserRole):
    session = _session()
    with patch("app.services.analytics_service.AnalyticsRepository") as Repo:
        for name in ("sum_usage_by_model", "count_requests_by_model"):
            setattr(Repo.return_value, name, AsyncMock(return_value={}))
        for name in ("count_active_users", "total_requests", "total_tokens"):
            setattr(Repo.return_value, name, AsyncMock(return_value=0))
        try:
            out = await AnalyticsService().get_analytics(
                session, period="2026-09", group_by="user", scope=scope, actor=_actor(role)
            )
        except Exception as exc:  # noqa: BLE001 - 테스트가 예외 종류를 본다
            out = exc
    return out, session, Repo.return_value


@pytest.mark.parametrize("role", [UserRole.TEAM_LEADER, UserRole.ADMIN])
@pytest.mark.parametrize("scope", BAD_SCOPES)
async def test_malformed_scope_is_rejected_before_any_query(scope, role):
    out, session, repo = await _get(scope, role)
    assert isinstance(out, ValidationError), f"{scope!r} → {out!r} (전사 범위로 떨어지면 안 된다)"
    session.execute.assert_not_awaited()
    repo.sum_usage_by_model.assert_not_awaited()


async def test_leader_all_and_own_team_still_work_and_stay_scoped():
    """대조군 — 정상 값은 그대로 동작하고 TEAM_LEADER 는 자기 팀으로 좁혀진다."""
    for scope in ("all", f"team:{OWN_TEAM}"):
        out, _, repo = await _get(scope, UserRole.TEAM_LEADER)
        assert not isinstance(out, Exception), (scope, out)
        args = repo.sum_usage_by_model.await_args.args
        assert args[2] == OWN_TEAM, f"{scope!r} 가 팀 범위로 좁혀지지 않았다: {args}"


async def test_leader_other_team_is_still_forbidden():
    out, session, _ = await _get(f"team:{uuid.uuid4()}", UserRole.TEAM_LEADER)
    assert isinstance(out, ForbiddenError)
    session.execute.assert_not_awaited()


async def test_admin_all_is_global():
    out, _, repo = await _get("all", UserRole.ADMIN)
    assert not isinstance(out, Exception), out
    assert repo.sum_usage_by_model.await_args.args[2] is None  # GLOBAL — scope_id 없음
