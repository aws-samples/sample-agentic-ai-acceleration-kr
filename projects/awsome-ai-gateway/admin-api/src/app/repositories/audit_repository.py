# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog, CacheInvalidationFailure


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_unresolved_failures(self) -> list[CacheInvalidationFailure]:
        stmt = select(CacheInvalidationFailure).where(
            CacheInvalidationFailure.resolved_at.is_(None)
        ).order_by(CacheInvalidationFailure.failed_at.asc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_audit_logs(
        self,
        *,
        actor_user_id: str | None = None,
        resource_type: str | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.timestamp.desc())
        if actor_user_id:
            stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
        if resource_type:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
