# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import KeyStatus, VirtualKey


class KeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, vk: VirtualKey) -> VirtualKey:
        self._session.add(vk)
        await self._session.flush()
        return vk

    async def get_by_id(self, key_id: uuid.UUID) -> VirtualKey | None:
        return await self._session.get(VirtualKey, key_id)

    async def expire_active_keys(self, user_id: uuid.UUID) -> int:
        stmt = (
            update(VirtualKey)
            .where(VirtualKey.user_id == user_id, VirtualKey.status == KeyStatus.ACTIVE)
            .values(status=KeyStatus.EXPIRED)
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def expire_and_create(
        self,
        user_id: uuid.UUID,
        new_vk: VirtualKey,
    ) -> tuple[int, uuid.UUID]:
        """Atomically expire all ACTIVE keys for user_id and insert new ACTIVE key.

        단일 SQL (CTE) 로 round-trip 1회. 같은 트랜잭션 내에서 호출해야 하며
        호출자가 commit 책임을 가짐.

        Returns:
            (expired_count, new_id)
        """
        from sqlalchemy import text

        sql = text(
            """
            WITH expired AS (
                UPDATE auth.virtual_keys
                SET status = 'EXPIRED'
                WHERE user_id = :user_id AND status = 'ACTIVE'
                RETURNING id
            ),
            inserted AS (
                INSERT INTO auth.virtual_keys (
                    id, user_id, key_value_encrypted, key_prefix,
                    status, expires_at, issued_at
                )
                VALUES (
                    :new_id, :user_id, :enc, :prefix,
                    'ACTIVE', :expires_at, :issued_at
                )
                RETURNING id
            )
            SELECT
                (SELECT COUNT(*) FROM expired) AS expired_count,
                (SELECT id FROM inserted) AS new_id
            """
        )
        result = await self._session.execute(
            sql,
            {
                "user_id": user_id,
                "new_id": new_vk.id,
                "enc": new_vk.key_value_encrypted,
                "prefix": new_vk.key_prefix,
                "expires_at": new_vk.expires_at,
                "issued_at": new_vk.issued_at,
            },
        )
        row = result.one()
        return int(row.expired_count), row.new_id

    async def revoke(self, key_id: uuid.UUID, revoked_by: uuid.UUID) -> VirtualKey | None:
        from datetime import datetime, timezone

        vk = await self.get_by_id(key_id)
        if vk is None:
            return None
        vk.status = KeyStatus.REVOKED
        vk.revoked_at = datetime.now(timezone.utc)
        vk.revoked_by = revoked_by
        return vk

    async def list_keys(
        self,
        *,
        user_id: uuid.UUID | None = None,
        team_id: uuid.UUID | None = None,
        status: KeyStatus | None = None,
        email: str | None = None,
        cursor: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[VirtualKey]:
        from app.models.auth import User

        stmt = select(VirtualKey).order_by(VirtualKey.created_at.desc())
        if user_id:
            stmt = stmt.where(VirtualKey.user_id == user_id)
        if status:
            stmt = stmt.where(VirtualKey.status == status)
        if cursor:
            stmt = stmt.where(VirtualKey.id < cursor)
        if team_id or email:
            stmt = stmt.join(User, VirtualKey.user_id == User.id)
            if team_id:
                stmt = stmt.where(User.team_id == team_id)
            if email:
                stmt = stmt.where(User.email.ilike(f"%{email}%"))
        stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_keys(
        self,
        *,
        user_id: uuid.UUID | None = None,
        team_id: uuid.UUID | None = None,
        status: KeyStatus | None = None,
        email: str | None = None,
    ) -> int:
        from app.models.auth import User

        stmt = select(func.count()).select_from(VirtualKey)
        if user_id:
            stmt = stmt.where(VirtualKey.user_id == user_id)
        if status:
            stmt = stmt.where(VirtualKey.status == status)
        if team_id or email:
            stmt = stmt.join(User, VirtualKey.user_id == User.id)
            if team_id:
                stmt = stmt.where(User.team_id == team_id)
            if email:
                stmt = stmt.where(User.email.ilike(f"%{email}%"))
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def list_active_for_user(self, user_id: uuid.UUID) -> list[VirtualKey]:
        stmt = select(VirtualKey).where(
            VirtualKey.user_id == user_id,
            VirtualKey.status == KeyStatus.ACTIVE,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

