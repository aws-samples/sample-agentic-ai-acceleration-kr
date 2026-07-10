# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.encryption import AESEncryptionService
from app.core.exceptions import NotFoundError
from app.models.auth import KeyStatus, User, VirtualKey
from app.services.key_service import KeyService, VK_PREFIX


@pytest.fixture
def key_service(encryption: AESEncryptionService, cache_mgr: CacheInvalidationManager) -> KeyService:
    return KeyService(encryption=encryption, cache_mgr=cache_mgr)


def _stub_user(user_id: uuid.UUID, team_id: uuid.UUID | None = None) -> MagicMock:
    u = MagicMock(spec=User)
    u.id = user_id
    u.team_id = team_id
    return u


def _patch_user_and_tam(user_id: uuid.UUID, team_id: uuid.UUID | None = None, aliases: list[str] | None = None):
    """UserRepository + TeamAllowedModelRepository 패치 컨텍스트 쌍 생성.

    issue_key가 이제 이 두 repo를 조회하므로 기존 tests도 mock 필요.
    """
    user_mock = patch("app.services.key_service.UserRepository")
    tam_mock = patch("app.services.key_service.TeamAllowedModelRepository")
    return user_mock, tam_mock, user_id, team_id, aliases or []


class TestIssueKey:
    def _mock_repo(self, MockRepo, *, expire_count=0):
        repo = MockRepo.return_value
        repo.expire_active_keys = AsyncMock(return_value=expire_count)
        repo.create = AsyncMock(side_effect=self._populate_dates)
        return repo

    @staticmethod
    def _populate_dates(vk):
        vk.issued_at = datetime.now(timezone.utc)
        return vk

    async def test_issue_key_generates_vk_prefix(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        user_id = uuid.uuid4()

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository") as MockTam:
            self._mock_repo(MockRepo)
            MockUserRepo.return_value.get_user = AsyncMock(return_value=_stub_user(user_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                result = await key_service.issue_key(mock_session, user_id=user_id, actor=admin_user)

        assert result.virtual_key.startswith(VK_PREFIX)
        assert len(result.virtual_key) == 67  # vk- + 64 hex chars
        assert result.key_prefix == result.virtual_key[:11]
        assert result.status == KeyStatus.ACTIVE

    async def test_issue_key_expires_existing_active_keys(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        user_id = uuid.uuid4()

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository") as MockTam:
            repo = self._mock_repo(MockRepo, expire_count=2)
            MockUserRepo.return_value.get_user = AsyncMock(return_value=_stub_user(user_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                await key_service.issue_key(mock_session, user_id=user_id, actor=admin_user)

        repo.expire_active_keys.assert_called_once_with(user_id)

    async def test_issue_key_defaults_to_24h_without_expires_at(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        user_id = uuid.uuid4()

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository") as MockTam:
            self._mock_repo(MockRepo)
            MockUserRepo.return_value.get_user = AsyncMock(return_value=_stub_user(user_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                result = await key_service.issue_key(mock_session, user_id=user_id, actor=admin_user)

        expected_min = datetime.now(timezone.utc) + timedelta(hours=23)
        expected_max = datetime.now(timezone.utc) + timedelta(hours=25)
        assert expected_min < result.expires_at < expected_max

    async def test_issue_key_encrypts_and_caches(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        user_id = uuid.uuid4()

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository") as MockTam:
            self._mock_repo(MockRepo)
            MockUserRepo.return_value.get_user = AsyncMock(return_value=_stub_user(user_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                result = await key_service.issue_key(mock_session, user_id=user_id, actor=admin_user)

        # key:vk:{hash} is stored with setex(ttl, user_id) — FR-2.2 TTL enforcement
        vk_setex_calls = [c for c in mock_redis.setex.call_args_list if c[0][0].startswith("key:vk:")]
        assert len(vk_setex_calls) == 1
        assert vk_setex_calls[0][0][1] > 0  # TTL must be positive

        # Encryption roundtrip: the VK stored in the model is encrypted
        created_vk = MockRepo.return_value.create.call_args[0][0]
        decrypted = key_service._encryption.decrypt(created_vk.key_value_encrypted)
        assert decrypted == result.virtual_key

    async def test_issue_key_redis_ttl_matches_vk_expiry(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        """FR-2.2: key:vk:{hash} TTL must match the VK lifetime."""
        user_id = uuid.uuid4()
        expiry = datetime.now(timezone.utc) + timedelta(hours=8)

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository") as MockTam:
            self._mock_repo(MockRepo)
            MockUserRepo.return_value.get_user = AsyncMock(return_value=_stub_user(user_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                await key_service.issue_key(mock_session, user_id=user_id, actor=admin_user, expires_at=expiry)

        vk_setex_calls = [c for c in mock_redis.setex.call_args_list if c[0][0].startswith("key:vk:")]
        ttl = vk_setex_calls[0][0][1]
        assert 28795 < ttl <= 28800  # ~8 hours, ±5s tolerance

        # AuthContext cache TTL is capped at min(300, ttl)
        ctx_setex_calls = [c for c in mock_redis.setex.call_args_list if c[0][0].startswith("key:cache:vk:")]
        ctx_ttl = ctx_setex_calls[0][0][1]
        assert ctx_ttl == 300  # 8h > 300s so cap applies

    async def test_issue_key_auth_cache_ttl_capped_when_vk_near_expiry(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        """When VK lifetime < 300s, AuthContext cache TTL must not exceed remaining lifetime."""
        user_id = uuid.uuid4()
        expiry = datetime.now(timezone.utc) + timedelta(seconds=60)

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository") as MockTam:
            self._mock_repo(MockRepo)
            MockUserRepo.return_value.get_user = AsyncMock(return_value=_stub_user(user_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                await key_service.issue_key(mock_session, user_id=user_id, actor=admin_user, expires_at=expiry)

        ctx_setex_calls = [c for c in mock_redis.setex.call_args_list if c[0][0].startswith("key:cache:vk:")]
        ctx_ttl = ctx_setex_calls[0][0][1]
        assert ctx_ttl <= 60  # must not exceed the 60s remaining lifetime

    async def test_issue_key_sso_session_expires_at_caps_expiry(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """FR-2.2: sso_session_expires_at shorter than rotation policy caps VK expiry."""
        user_id = uuid.uuid4()
        sso_expiry = datetime.now(timezone.utc) + timedelta(hours=8)

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository") as MockTam:
            self._mock_repo(MockRepo)  # policy=None → default 90 days
            MockUserRepo.return_value.get_user = AsyncMock(return_value=_stub_user(user_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                result = await key_service.issue_key(
                    mock_session,
                    user_id=user_id,
                    actor=admin_user,
                    sso_session_expires_at=sso_expiry,
                )

        # 90-day policy > 8h SSO session, so VK expiry must equal SSO expiry
        assert abs((result.expires_at - sso_expiry).total_seconds()) < 2

    async def test_issue_key_sso_expiry_ignored_when_longer_than_policy(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """sso_session_expires_at longer than rotation policy leaves policy expiry unchanged."""
        user_id = uuid.uuid4()
        policy = MagicMock(spec=RotationPolicy)
        policy.expiry_days = 1
        sso_expiry = datetime.now(timezone.utc) + timedelta(days=30)

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository") as MockTam:
            self._mock_repo(MockRepo, policy=policy)
            MockUserRepo.return_value.get_user = AsyncMock(return_value=_stub_user(user_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                result = await key_service.issue_key(
                    mock_session,
                    user_id=user_id,
                    actor=admin_user,
                    sso_session_expires_at=sso_expiry,
                )

        expected_min = datetime.now(timezone.utc) + timedelta(hours=23)
        expected_max = datetime.now(timezone.utc) + timedelta(hours=25)
        assert expected_min < result.expires_at < expected_max


    async def test_issue_key_snapshots_team_allowed_models_into_cache(
        self,
        key_service: KeyService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        """FR-2.6: VK 발급 시 user의 team_allowed_models를 AuthContext 캐시에 주입."""
        import json

        user_id = uuid.uuid4()
        team_id = uuid.uuid4()

        mock_redis.setex = AsyncMock()
        mock_redis.sadd = AsyncMock()

        def _populate_dates(vk):
            vk.issued_at = datetime.now(timezone.utc)
            return vk

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository") as MockTam:
            repo = MockRepo.return_value
            repo.expire_active_keys = AsyncMock(return_value=0)
            repo.create = AsyncMock(side_effect=_populate_dates)
            MockUserRepo.return_value.get_user = AsyncMock(
                return_value=_stub_user(user_id, team_id)
            )
            MockTam.return_value.list_by_team = AsyncMock(
                return_value=["claude-haiku", "claude-sonnet"]
            )

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                await key_service.issue_key(mock_session, user_id=user_id, actor=admin_user)

        # AuthContext 캐시에 allowed_models 스냅샷 포함
        setex_calls = mock_redis.setex.call_args_list
        auth_cache_call = next(c for c in setex_calls if c[0][0].startswith("key:cache:vk:"))
        payload = json.loads(auth_cache_call[0][2])
        assert payload["allowed_models"] == ["claude-haiku", "claude-sonnet"]
        assert payload["team_id"] == str(team_id)

        # Reverse index에 VK hash 추가 (팀 범위 invalidate용)
        mock_redis.sadd.assert_called_once()
        assert mock_redis.sadd.call_args[0][0] == f"team:vk_hashes:{team_id}"

    async def test_issue_key_no_team_means_allowed_models_none(
        self,
        key_service: KeyService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        """team_id 없는 user → allowed_models=None (전체 허용)."""
        import json

        user_id = uuid.uuid4()
        mock_redis.setex = AsyncMock()
        mock_redis.sadd = AsyncMock()

        def _populate_dates(vk):
            vk.issued_at = datetime.now(timezone.utc)
            return vk

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo, \
             patch("app.services.key_service.TeamAllowedModelRepository"):
            repo = MockRepo.return_value
            repo.expire_active_keys = AsyncMock(return_value=0)
            repo.create = AsyncMock(side_effect=_populate_dates)
            MockUserRepo.return_value.get_user = AsyncMock(
                return_value=_stub_user(user_id, team_id=None)
            )

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                await key_service.issue_key(mock_session, user_id=user_id, actor=admin_user)

        setex_calls = mock_redis.setex.call_args_list
        auth_cache_call = next(c for c in setex_calls if c[0][0].startswith("key:cache:vk:"))
        payload = json.loads(auth_cache_call[0][2])
        assert payload["allowed_models"] is None
        assert payload["team_id"] == ""
        mock_redis.sadd.assert_not_called()


class TestRevokeKey:
    async def test_revoke_key_not_found_raises(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        key_id = uuid.uuid4()

        with patch("app.services.key_service.KeyRepository") as MockRepo:
            repo = MockRepo.return_value
            repo.revoke = AsyncMock(return_value=None)

            with pytest.raises(NotFoundError):
                await key_service.revoke_key(mock_session, key_id=key_id, actor=admin_user)

    async def test_revoke_key_invalidates_cache(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        key_id = uuid.uuid4()
        raw_key = "vk-" + "a" * 64
        encrypted = key_service._encryption.encrypt(raw_key)

        vk = MagicMock(spec=VirtualKey)
        vk.key_value_encrypted = encrypted
        vk.user_id = uuid.uuid4()

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.UserRepository") as MockUserRepo:
            repo = MockRepo.return_value
            repo.revoke = AsyncMock(return_value=vk)
            MockUserRepo.return_value.get_user = AsyncMock(return_value=_stub_user(vk.user_id))

            with patch("app.services.key_service.audit_logger") as mock_audit:
                mock_audit.log = AsyncMock()
                await key_service.revoke_key(mock_session, key_id=key_id, actor=admin_user)

        # Redis DEL was called for two keys
        assert mock_redis.delete.call_count >= 2


class TestListActiveVkHashesForUser:
    async def test_list_active_vk_hashes_for_user_returns_hashes(
        self, key_service: KeyService, mock_session: AsyncMock
    ):
        user_id = uuid.uuid4()
        vk1 = MagicMock(id=uuid.uuid4(), key_value_encrypted=b"enc-1")
        vk2 = MagicMock(id=uuid.uuid4(), key_value_encrypted=b"enc-2")

        with patch("app.services.key_service.KeyRepository") as KRepo:
            KRepo.return_value.list_active_for_user = AsyncMock(return_value=[vk1, vk2])
            key_service._encryption.decrypt = MagicMock(side_effect=lambda b: f"raw-{b.decode()}")

            hashes = await key_service.list_active_vk_hashes_for_user(mock_session, user_id)

        expected = [
            hashlib.sha256("raw-enc-1".encode()).hexdigest(),
            hashlib.sha256("raw-enc-2".encode()).hexdigest(),
        ]
        assert hashes == expected

    async def test_list_active_vk_hashes_for_user_empty(
        self, key_service: KeyService, mock_session: AsyncMock
    ):
        user_id = uuid.uuid4()
        with patch("app.services.key_service.KeyRepository") as KRepo:
            KRepo.return_value.list_active_for_user = AsyncMock(return_value=[])
            hashes = await key_service.list_active_vk_hashes_for_user(mock_session, user_id)
        assert hashes == []

    async def test_list_active_vk_hashes_for_user_skips_decrypt_failures(
        self, key_service: KeyService, mock_session: AsyncMock
    ):
        user_id = uuid.uuid4()
        vk1 = MagicMock(id=uuid.uuid4(), key_value_encrypted=b"good")
        vk2 = MagicMock(id=uuid.uuid4(), key_value_encrypted=b"bad")

        with patch("app.services.key_service.KeyRepository") as KRepo:
            KRepo.return_value.list_active_for_user = AsyncMock(return_value=[vk1, vk2])

            def _decrypt(b: bytes) -> str:
                if b == b"bad":
                    raise ValueError("decrypt error")
                return "raw-good"
            key_service._encryption.decrypt = MagicMock(side_effect=_decrypt)

            hashes = await key_service.list_active_vk_hashes_for_user(mock_session, user_id)

        assert hashes == [hashlib.sha256("raw-good".encode()).hexdigest()]

    @pytest.mark.asyncio
    async def test_list_active_vk_hashes_excludes_expired_keys(
        self, key_service: KeyService, mock_session: AsyncMock
    ):
        """EXPIRED keys (status=EXPIRED, revoked_at=NULL) must not be returned.

        The status=ACTIVE filter is enforced at the repository layer.
        This test verifies that list_active_vk_hashes_for_user passes through
        only what the repo returns — i.e. the repo is the single gating point
        for the status filter, and callers receive only hashes for ACTIVE keys.
        """
        user_id = uuid.uuid4()
        active_vk = MagicMock(id=uuid.uuid4(), key_value_encrypted=b"active")

        with patch("app.services.key_service.KeyRepository") as KRepo:
            # Repo returns only the active VK — expired ones are filtered at DB level.
            KRepo.return_value.list_active_for_user = AsyncMock(return_value=[active_vk])
            key_service._encryption.decrypt = MagicMock(return_value="raw-active")
            hashes = await key_service.list_active_vk_hashes_for_user(mock_session, user_id)

        assert hashes == [hashlib.sha256("raw-active".encode()).hexdigest()]


class TestForceReauthTeam:
    """PR 4-A: 팀 멤버 전원 ACTIVE VK 일괄 revoke.

    `revoke_key` 루프를 검증. revoke_key 자체는 다른 테스트에서 이미 커버되므로
    여기서는 (1) repo 에 status=ACTIVE 필터 전달, (2) 각 VK 에 대해 revoke_key 호출,
    (3) revoked_count 반환, (4) audit log 한 건만 집중.
    """

    @pytest.mark.asyncio
    async def test_force_reauth_team_revokes_all_active_keys(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        vk1 = MagicMock(id=uuid.uuid4())
        vk2 = MagicMock(id=uuid.uuid4())
        vk3 = MagicMock(id=uuid.uuid4())

        with patch("app.services.key_service.KeyRepository") as KRepo:
            KRepo.return_value.list_keys = AsyncMock(return_value=[vk1, vk2, vk3])
            with patch.object(key_service, "revoke_key", new=AsyncMock()) as mock_revoke:
                with patch("app.services.key_service.audit_logger.log", new=AsyncMock()):
                    count = await key_service.force_reauth_team(
                        mock_session, team_id=team_id, actor=admin_user
                    )

        assert count == 3
        # list_keys 는 team_id + status=ACTIVE 로 호출됐어야 함
        call_kwargs = KRepo.return_value.list_keys.call_args.kwargs
        assert call_kwargs["team_id"] == team_id
        assert call_kwargs["status"] == KeyStatus.ACTIVE
        # revoke_key 가 각 VK 에 대해 한 번씩 호출됨
        assert mock_revoke.call_count == 3
        revoked_key_ids = {c.kwargs["key_id"] for c in mock_revoke.call_args_list}
        assert revoked_key_ids == {vk1.id, vk2.id, vk3.id}

    @pytest.mark.asyncio
    async def test_force_reauth_team_returns_zero_when_no_active_keys(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()

        with patch("app.services.key_service.KeyRepository") as KRepo:
            KRepo.return_value.list_keys = AsyncMock(return_value=[])
            with patch.object(key_service, "revoke_key", new=AsyncMock()) as mock_revoke:
                with patch("app.services.key_service.audit_logger.log", new=AsyncMock()):
                    count = await key_service.force_reauth_team(
                        mock_session, team_id=team_id, actor=admin_user
                    )

        assert count == 0
        mock_revoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_reauth_team_continues_on_single_revoke_failure(
        self, key_service: KeyService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """한 건 revoke 실패해도 나머지 처리 계속, count 는 성공한 것만."""
        team_id = uuid.uuid4()
        vk_ok1 = MagicMock(id=uuid.uuid4())
        vk_fail = MagicMock(id=uuid.uuid4())
        vk_ok2 = MagicMock(id=uuid.uuid4())

        async def revoke_side_effect(*args, **kwargs):
            if kwargs["key_id"] == vk_fail.id:
                raise RuntimeError("redis down")

        with patch("app.services.key_service.KeyRepository") as KRepo:
            KRepo.return_value.list_keys = AsyncMock(return_value=[vk_ok1, vk_fail, vk_ok2])
            with patch.object(key_service, "revoke_key", new=AsyncMock(side_effect=revoke_side_effect)) as mock_revoke:
                with patch("app.services.key_service.audit_logger.log", new=AsyncMock()):
                    count = await key_service.force_reauth_team(
                        mock_session, team_id=team_id, actor=admin_user
                    )

        assert count == 2  # 성공한 2건만
        assert mock_revoke.call_count == 3  # 3건 모두 시도함
