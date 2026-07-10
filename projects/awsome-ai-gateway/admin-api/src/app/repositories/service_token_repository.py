# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import ServiceToken


class ServiceTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, tok: ServiceToken) -> ServiceToken:
        self._session.add(tok)
        await self._session.flush()
        return tok

    async def get_by_hash(self, token_hash: str) -> ServiceToken | None:
        stmt = select(ServiceToken).where(ServiceToken.token_hash == token_hash)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, token_id: uuid.UUID) -> ServiceToken | None:
        stmt = select(ServiceToken).where(ServiceToken.id == token_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[ServiceToken]:
        stmt = select(ServiceToken).order_by(ServiceToken.created_at.desc())
        return list((await self._session.execute(stmt)).scalars().all())

    async def set_revoked_at(self, token_id: uuid.UUID, revoked_at: datetime) -> None:
        await self._session.execute(
            update(ServiceToken)
            .where(ServiceToken.id == token_id)
            .values(revoked_at=revoked_at)
        )
        await self._session.flush()
