# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Per-entity Cognito sync (sync_user / sync_group) — 260626_comm_customer 항목1-a.

핵심 검증:
- DB 에 없는 사용자도 신규 생성 (고객 명시 케이스)
- 기존(sso_subject hit) → update / 재생성(email reconcile) → 새 sub 갱신
- ★ 전역 reconciliation(deactivate-missing / stale-team)을 절대 수행하지 않음
  (sync_all 과 달리 repo.list_users / list_all_teams 를 호출하면 안 됨)
- Cognito 에 없는 사용자 → 에러 대신 soft-delete(is_active=False), user_id 반환
- 그룹 sync: 팀 확보 + 멤버 upsert
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.auth import UserRole
from app.services.cognito_sync_service import CognitoSyncService


def _settings(monkeypatch):
    s = MagicMock()
    s.COGNITO_USER_POOL_ID = "pool-1"
    s.DEFAULT_TEAM_ID = str(uuid.uuid4())
    s.OIDC_PROVIDER_NAME = "cognito"
    s.OIDC_GROUP_PREFIX = "Claude_"
    s.ADMIN_EMAILS = []
    s.ADMIN_GROUPS = []
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "get_settings", lambda: s)
    return s


def _admin_get_user(sub, email, name="N", enabled=True):
    return {
        "Username": email,
        "Enabled": enabled,
        "UserAttributes": [
            {"Name": "sub", "Value": sub},
            {"Name": "email", "Value": email},
            {"Name": "name", "Value": name},
        ],
    }


@pytest.mark.asyncio
async def test_sync_user_creates_when_absent(monkeypatch):
    """DB 에 없는 사용자 → 신규 생성 (고객 핵심 요구)."""
    s = _settings(monkeypatch)
    session = AsyncMock()
    repo = MagicMock()
    repo.get_by_sso_subject = AsyncMock(return_value=None)
    repo.get_by_email = AsyncMock(return_value=None)  # DB 에 전혀 없음
    repo.create_user = AsyncMock()
    # 전역 sweep 함수 — 호출되면 테스트 실패하도록 감지
    repo.list_users = AsyncMock(return_value=[])
    repo.list_all_teams = AsyncMock(return_value=[])

    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(return_value=_admin_get_user("NEW-SUB", "new@x.com"))
    svc._list_groups_for_user = MagicMock(return_value=[])  # 그룹 없음 → DEFAULT_TEAM

    result = await svc.sync_user(session, "new@x.com")

    repo.create_user.assert_awaited_once()
    assert result.users_created == 1
    # ★ 핵심 안전장치: 전역 deactivate-missing sweep(repo.list_users 로 전체 유저
    #   조회 후 비활성화) 절대 미수행. (_ensure_team 은 list_all_teams 로 팀을 '조회'
    #   할 수 있으므로 — 그건 무해한 read — list_all_teams 미호출은 단언하지 않는다.
    #   위험한 불변식은 'deactivation sweep 없음'이다.)
    repo.list_users.assert_not_called()
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_sync_user_updates_existing(monkeypatch):
    """sso_subject hit → 기존 row update, create 안 함."""
    s = _settings(monkeypatch)
    session = AsyncMock()
    existing = MagicMock()
    existing.id = uuid.uuid4(); existing.sso_subject = "SUB-1"
    existing.email = "old@x.com"; existing.display_name = "Old"
    existing.role = UserRole.DEVELOPER; existing.team_id = uuid.UUID(s.DEFAULT_TEAM_ID)
    existing.is_active = True
    repo = MagicMock()
    repo.get_by_sso_subject = AsyncMock(return_value=existing)
    repo.create_user = AsyncMock()
    repo.list_users = AsyncMock(return_value=[])
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(return_value=_admin_get_user("SUB-1", "new@x.com", "New"))
    svc._list_groups_for_user = MagicMock(return_value=[])

    result = await svc.sync_user(session, "new@x.com")

    repo.create_user.assert_not_called()
    assert existing.email == "new@x.com"  # updated
    assert result.users_updated == 1
    repo.list_users.assert_not_called()  # 전역 sweep 없음


@pytest.mark.asyncio
async def test_sync_user_reconciles_recreated_sub(monkeypatch):
    """재생성(새 sub, 같은 email): create 안 하고 기존 row 의 sso_subject 갱신."""
    s = _settings(monkeypatch)
    session = AsyncMock()
    existing = MagicMock()
    existing.id = uuid.uuid4(); existing.sso_subject = "OLD-SUB"
    existing.email = "a@x.com"; existing.display_name = "A"
    existing.role = UserRole.DEVELOPER; existing.team_id = uuid.UUID(s.DEFAULT_TEAM_ID)
    existing.is_active = True
    repo = MagicMock()
    repo.get_by_sso_subject = AsyncMock(return_value=None)  # 새 sub miss
    repo.get_by_email = AsyncMock(return_value=existing)     # email hit
    repo.create_user = AsyncMock()
    repo.list_users = AsyncMock(return_value=[])
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(return_value=_admin_get_user("NEW-SUB", "a@x.com"))
    svc._list_groups_for_user = MagicMock(return_value=[])

    await svc.sync_user(session, "a@x.com")

    repo.create_user.assert_not_called()
    assert existing.sso_subject == "NEW-SUB"  # reconciled


@pytest.mark.asyncio
async def test_sync_user_deactivates_oidc_user_when_missing_in_cognito(monkeypatch):
    """Cognito 에 없는 username(=삭제됨) + DB 에 활성 OIDC 유저 → is_active False,
    user_id 반환, users_deactivated=1, 에러 아님 (고객 항목1 핵심)."""
    s = _settings(monkeypatch)
    session = AsyncMock()
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.provider = s.OIDC_PROVIDER_NAME
    existing.is_active = True
    repo = MagicMock()
    repo.get_by_email = AsyncMock(return_value=existing)
    repo.create_user = AsyncMock()
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(return_value=None)  # Cognito 삭제됨

    result = await svc.sync_user(session, "gone@x.com")

    assert existing.is_active is False
    assert result.users_deactivated == 1
    assert result.user_id == str(existing.id)
    assert result.errors == []
    repo.create_user.assert_not_called()
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_sync_user_missing_in_cognito_and_db_is_noop(monkeypatch):
    """Cognito 에도 DB 에도 없음 → no-op. user_id=None, deactivated=0, 에러 아님."""
    _settings(monkeypatch)
    session = AsyncMock()
    repo = MagicMock()
    repo.get_by_email = AsyncMock(return_value=None)
    repo.create_user = AsyncMock()
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(return_value=None)

    result = await svc.sync_user(session, "ghost@x.com")

    assert result.user_id is None
    assert result.users_deactivated == 0
    assert result.errors == []
    repo.create_user.assert_not_called()


@pytest.mark.asyncio
async def test_sync_user_missing_in_cognito_skips_non_oidc(monkeypatch):
    """Cognito 없음 + DB 유저가 비-OIDC(수동/서비스 계정) → 비활성화 안 함.
    단 찾았으므로 user_id 는 반환(email 충돌 사고 방지 안전장치)."""
    _settings(monkeypatch)
    session = AsyncMock()
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.provider = "manual"  # 비-OIDC
    existing.is_active = True
    repo = MagicMock()
    repo.get_by_email = AsyncMock(return_value=existing)
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(return_value=None)

    result = await svc.sync_user(session, "admin@x.com")

    assert existing.is_active is True  # 건드리지 않음
    assert result.users_deactivated == 0
    assert result.user_id == str(existing.id)


@pytest.mark.asyncio
async def test_sync_user_missing_in_cognito_already_inactive(monkeypatch):
    """Cognito 없음 + 이미 비활성 OIDC 유저 → deactivated 카운트 안 올림, user_id 반환."""
    s = _settings(monkeypatch)
    session = AsyncMock()
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.provider = s.OIDC_PROVIDER_NAME
    existing.is_active = False  # 이미 비활성
    repo = MagicMock()
    repo.get_by_email = AsyncMock(return_value=existing)
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(return_value=None)

    result = await svc.sync_user(session, "gone@x.com")

    assert result.users_deactivated == 0
    assert result.user_id == str(existing.id)


@pytest.mark.asyncio
async def test_sync_user_returns_user_id_on_create(monkeypatch):
    """정상 경로(생성) → 생성된 유저의 user_id 를 응답에 담는다(후속 처리용)."""
    s = _settings(monkeypatch)
    session = AsyncMock()
    created = {}

    async def _capture_create(user):
        created["user"] = user
        return user

    repo = MagicMock()
    repo.get_by_sso_subject = AsyncMock(return_value=None)
    repo.get_by_email = AsyncMock(return_value=None)
    repo.create_user = AsyncMock(side_effect=_capture_create)
    repo.list_users = AsyncMock(return_value=[])
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(return_value=_admin_get_user("NEW-SUB", "new@x.com"))
    svc._list_groups_for_user = MagicMock(return_value=[])

    result = await svc.sync_user(session, "new@x.com")

    assert result.users_created == 1
    assert result.user_id == str(created["user"].id)


@pytest.mark.asyncio
async def test_sync_user_returns_user_id_on_update(monkeypatch):
    """정상 경로(갱신) → 기존 유저의 user_id 를 응답에 담는다."""
    s = _settings(monkeypatch)
    session = AsyncMock()
    existing = MagicMock()
    existing.id = uuid.uuid4(); existing.sso_subject = "SUB-1"
    existing.email = "old@x.com"; existing.display_name = "Old"
    existing.role = UserRole.DEVELOPER; existing.team_id = uuid.UUID(s.DEFAULT_TEAM_ID)
    existing.is_active = True
    repo = MagicMock()
    repo.get_by_sso_subject = AsyncMock(return_value=existing)
    repo.create_user = AsyncMock()
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(return_value=_admin_get_user("SUB-1", "new@x.com", "New"))
    svc._list_groups_for_user = MagicMock(return_value=[])

    result = await svc.sync_user(session, "new@x.com")

    assert result.user_id == str(existing.id)


@pytest.mark.asyncio
async def test_sync_group_upserts_members_no_global_cleanup(monkeypatch):
    """그룹 sync: 팀 확보 + 멤버 upsert. 전역 정리(list_all_teams sweep) 미수행."""
    s = _settings(monkeypatch)
    session = AsyncMock()
    team = MagicMock(); team.id = uuid.uuid4()
    repo = MagicMock()
    repo.get_by_sso_subject = AsyncMock(return_value=None)
    repo.get_by_email = AsyncMock(return_value=None)
    repo.create_user = AsyncMock()
    repo.list_users = AsyncMock(return_value=[])
    repo.list_all_teams = AsyncMock(return_value=[])
    import app.services.cognito_sync_service as mod
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    svc = CognitoSyncService(MagicMock())
    svc._ensure_team = AsyncMock(return_value=team)
    svc._list_users_in_group = MagicMock(return_value=[
        {"Username": "m1@x.com", "Enabled": True, "Attributes": [
            {"Name": "sub", "Value": "S1"}, {"Name": "email", "Value": "m1@x.com"}]},
        {"Username": "m2@x.com", "Enabled": True, "Attributes": [
            {"Name": "sub", "Value": "S2"}, {"Name": "email", "Value": "m2@x.com"}]},
    ])

    result = await svc.sync_group(session, "Claude_dept_team")

    assert repo.create_user.await_count == 2
    assert result.users_created == 2
    assert result.groups_synced == 1
    repo.list_users.assert_not_called()  # ★ deactivate-missing sweep 없음 (위험 불변식)
