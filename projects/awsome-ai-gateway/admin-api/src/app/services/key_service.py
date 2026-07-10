# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.encryption import AESEncryptionService
from app.core.exceptions import NotFoundError
from app.models.auth import KeyStatus, User, VirtualKey
from app.repositories.key_repository import KeyRepository
from app.repositories.model_repository import TeamAllowedModelRepository
from app.repositories.user_repository import UserRepository
from app.schemas.keys import KeyCreateResponse, KeyResponse

logger = structlog.get_logger()

VK_PREFIX = "vk-"
VK_RANDOM_BYTES = 32  # 32 bytes = 64 hex chars → total 67 chars with prefix
VK_AUTH_CACHE_TTL = 300  # gateway-proxy auth_service.VK_CACHE_TTL와 일치
VK_DEFAULT_TTL_HOURS = 24  # 호출자가 expires_at 을 지정하지 않은 경우의 기본값


class KeyService:
    def __init__(
        self,
        encryption: AESEncryptionService,
        cache_mgr: CacheInvalidationManager,
    ) -> None:
        self._encryption = encryption
        self._cache_mgr = cache_mgr

    async def issue_key(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        actor: CurrentUser,
        expires_at: datetime | None = None,
        sso_session_expires_at: datetime | None = None,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
        user: User | None = None,
    ) -> KeyCreateResponse:
        """Issue a new Virtual Key for user_id.

        ``user`` optional: 호출자가 이미 같은 session 에서 조회한 User 객체를 전달하면
        내부 ``get_user(user_id)`` 재조회를 생략. 전달하지 않으면 기존과 동일 동작.
        10k boot storm 부하테스트에서 요청당 DB roundtrip 을 줄여 conn 점유 시간 감소.
        """
        repo = KeyRepository(session)

        # Generate VK: vk- + 32-byte random hex (먼저 생성 — CTE 한 번에 expire+insert)
        raw_key = VK_PREFIX + secrets.token_hex(VK_RANDOM_BYTES)
        key_prefix = raw_key[:11]  # "vk-a3b9c1d2"

        # AES-256-GCM encrypt
        encrypted = self._encryption.encrypt(raw_key)

        if expires_at is None:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=VK_DEFAULT_TTL_HOURS)

        # FR-2.2: cap VK expiry to SSO session expiry if provided
        if sso_session_expires_at and sso_session_expires_at < expires_at:
            logger.info(
                "key.sso_expiry_applied",
                user_id=str(user_id),
                policy_expires_at=expires_at.isoformat(),
                sso_session_expires_at=sso_session_expires_at.isoformat(),
            )
            expires_at = sso_session_expires_at
        else:
            logger.info(
                "key.expiry_set",
                user_id=str(user_id),
                expires_at=expires_at.isoformat(),
                sso_session_expires_at=sso_session_expires_at.isoformat() if sso_session_expires_at else None,
            )

        # issued_at: CTE path uses raw SQL (no ORM refresh), so set explicitly.
        # ORM `repo.create()` path would auto-fill from server_default; here we don't.
        now = datetime.now(timezone.utc)
        vk = VirtualKey(
            id=uuid.uuid4(),
            user_id=user_id,
            key_value_encrypted=encrypted,
            key_prefix=key_prefix,
            status=KeyStatus.ACTIVE,
            expires_at=expires_at,
            issued_at=now,
        )

        # BR-KEY-01 + INSERT 를 단일 CTE 로 (B: round-trip 절감)
        expired_count, _ = await repo.expire_and_create(user_id, vk)
        if expired_count > 0:
            logger.info("key.expired_existing", user_id=str(user_id), count=expired_count)

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        seconds_until_expiry = max(1, int((expires_at - now).total_seconds()))

        # team_allowed_models 스냅샷을 AuthContext 캐시에 주입.
        # 호출자가 user 를 전달했으면 재조회 skip (같은 session identity map 객체 재사용).
        if user is None:
            user_repo = UserRepository(session)
            user = await user_repo.get_user(user_id)
            if user is None:
                raise NotFoundError("User", str(user_id))

        allowed_models: list[str] | None = None
        if user.team_id is not None:
            tam_repo = TeamAllowedModelRepository(session)
            team_aliases = await tam_repo.list_by_team(user.team_id)
            # 엔트리 0개 → None (전체 허용). 엔트리 존재 → 화이트리스트.
            allowed_models = team_aliases if team_aliases else None

        auth_context_payload = {
            "user_id": str(user.id),
            "team_id": str(user.team_id) if user.team_id else "",
            "dept_id": "",
            "roles": ["USER"],
            "auth_type": "VIRTUAL_KEY",
            "key_id": None,
            "allowed_models": allowed_models,
            "sso_subject": user.sso_subject,
        }
        auth_cache_ttl = min(VK_AUTH_CACHE_TTL, seconds_until_expiry)

        # Redis 작업 3건 (VK lookup, auth context, team reverse index) 을 pipeline 으로
        # 묶어 round-trip 절감. 각 key 가 독립적이라 순서/원자성 무관.
        redis = self._cache_mgr._redis
        pipe = redis.pipeline(transaction=False)
        pipe.setex(f"key:vk:{key_hash}", seconds_until_expiry, f"{user_id}")
        pipe.setex(
            f"key:cache:vk:{key_hash}",
            auth_cache_ttl,
            json.dumps(auth_context_payload),
        )
        if user.team_id is not None:
            pipe.sadd(f"team:vk_hashes:{user.team_id}", key_hash)
        await pipe.execute()

        # Audit log
        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CREATE_KEY",
            resource_type="VirtualKey",
            resource_id=str(vk.id),
            changes={"after": {"user_id": str(user_id), "key_prefix": key_prefix}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return KeyCreateResponse(
            key_id=str(vk.id),
            key_prefix=key_prefix,
            user_id=str(user_id),
            status=vk.status,
            created_at=vk.issued_at,
            expires_at=vk.expires_at,
            virtual_key=raw_key,
        )

    async def revoke_key(
        self,
        session: AsyncSession,
        *,
        key_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        repo = KeyRepository(session)
        vk = await repo.revoke(key_id, actor.user_id)
        if vk is None:
            raise NotFoundError("VirtualKey", str(key_id))

        # Invalidate Redis cache
        # We need the hash of the raw key, but we only have encrypted.
        # Decrypt to get raw key, then hash it.
        raw_key = self._encryption.decrypt(vk.key_value_encrypted)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        await self._cache_mgr.invalidate(
            [f"key:vk:{key_hash}", f"key:cache:vk:{key_hash}"],
            session=session,
        )

        # Remove from team VK hash reverse index (FR-2.6)
        user_repo = UserRepository(session)
        user = await user_repo.get_user(vk.user_id)
        if user is not None and user.team_id is not None:
            try:
                await self._cache_mgr._redis.srem(
                    f"team:vk_hashes:{user.team_id}", key_hash
                )
            except Exception:
                logger.warning("vk_reverse_index.srem_failed", user_id=str(vk.user_id), exc_info=True)

        # BR-KEY-04: Audit log for revocation
        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="REVOKE_KEY",
            resource_type="VirtualKey",
            resource_id=str(key_id),
            changes={"before": {"status": "ACTIVE"}, "after": {"status": "REVOKED"}},
            ip_address=ip_address,
            request_id=request_id,
        )

    async def list_keys(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | None = None,
        team_id: uuid.UUID | None = None,
        status: KeyStatus | None = None,
        email: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[KeyResponse], bool]:
        from sqlalchemy import select

        from app.models.auth import User

        repo = KeyRepository(session)
        cursor_uuid = uuid.UUID(cursor) if cursor else None
        keys = await repo.list_keys(
            user_id=user_id,
            team_id=team_id,
            status=status,
            email=email,
            cursor=cursor_uuid,
            limit=limit + 1,
        )
        has_more = len(keys) > limit
        if has_more:
            keys = keys[:limit]

        email_by_user: dict[uuid.UUID, str] = {}
        if keys:
            uids = {vk.user_id for vk in keys}
            rows = await session.execute(
                select(User.id, User.email).where(User.id.in_(uids))
            )
            email_by_user = {uid: em for uid, em in rows.all()}

        items = [
            KeyResponse(
                key_id=str(vk.id),
                key_prefix=vk.key_prefix,
                user_id=str(vk.user_id),
                user_email=email_by_user.get(vk.user_id),
                status=vk.status,
                issued_at=vk.issued_at,
                expires_at=vk.expires_at,
                last_used_at=vk.last_used_at,
                created_at=vk.created_at,
            )
            for vk in keys
        ]
        return items, has_more

    async def count_keys(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | None = None,
        team_id: uuid.UUID | None = None,
        status: KeyStatus | None = None,
        email: str | None = None,
    ) -> int:
        repo = KeyRepository(session)
        return await repo.count_keys(
            user_id=user_id, team_id=team_id, status=status, email=email
        )

    async def force_reauth_team(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> int:
        """팀 멤버 전원의 ACTIVE VK 를 일괄 revoke.

        사용 시나리오:
        - 오프보딩/보안 사고 등 즉시 차단이 필요할 때
        - Cognito group 변경을 1h TTL 자연 만료보다 빠르게 반영하고 싶을 때

        사용자 측 영향: 다음 호출이 401 로 차단됨. Claude Code 재실행 시
        vk-cache 만료 → apiKeyHelper exchange → 새 VK (현재 Cognito groups 기준)
        발급 → 자동 복구. 사용자에게 재실행 안내가 필요함을 UI 에서 명시.

        Returns:
            revoked VK 개수.
        """
        repo = KeyRepository(session)
        # 팀 멤버의 ACTIVE VK 만 대상 (이미 REVOKED/EXPIRED 는 무의미).
        # limit=1000: 단일 팀 멤버 수 상한 가정. 넘으면 FK 로그 필요 (MVP 단계).
        keys = await repo.list_keys(
            team_id=team_id, status=KeyStatus.ACTIVE, cursor=None, limit=1000
        )

        revoked = 0
        for vk in keys:
            try:
                await self.revoke_key(
                    session,
                    key_id=vk.id,
                    actor=actor,
                    ip_address=ip_address,
                    request_id=request_id,
                )
                revoked += 1
            except Exception:
                logger.exception(
                    "force_reauth_team.revoke_failed",
                    team_id=str(team_id),
                    key_id=str(vk.id),
                )
                # 한 건 실패해도 나머지 계속 처리

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="FORCE_REAUTH_TEAM",
            resource_type="Team",
            resource_id=str(team_id),
            changes={"after": {"revoked_count": revoked}},
            ip_address=ip_address,
            request_id=request_id,
        )

        logger.info(
            "force_reauth_team.completed",
            team_id=str(team_id),
            revoked_count=revoked,
        )
        return revoked

    async def list_active_vk_hashes_for_user(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> list[str]:
        """Return SHA256 hashes of all non-revoked VKs owned by user_id.

        Mirrors the decrypt-then-hash pattern used by `revoke_key` (line 182-183)
        since `auth.virtual_keys` does not store the hash directly.
        """
        repo = KeyRepository(session)
        rows = await repo.list_active_for_user(user_id)
        hashes: list[str] = []
        for vk in rows:
            try:
                raw = self._encryption.decrypt(vk.key_value_encrypted)
                hashes.append(hashlib.sha256(raw.encode()).hexdigest())
            except Exception:
                logger.warning(
                    "vk_hash_derive_failed", vk_id=str(vk.id), user_id=str(user_id),
                    exc_info=True,
                )
                continue
        return hashes

