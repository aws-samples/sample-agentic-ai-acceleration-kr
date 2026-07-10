# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import UserAllowedClient


class UserAllowedClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_user(self, user_id: uuid.UUID) -> list[str]:
        stmt = select(UserAllowedClient.client).where(UserAllowedClient.user_id == user_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def replace_for_user(
        self, user_id: uuid.UUID, clients: list[str], created_by: uuid.UUID
    ) -> None:
        # replace-all: 0개면 모두 삭제(=전체 허용). team_allowed_models 와 동일 의미론.
        await self._session.execute(
            delete(UserAllowedClient).where(UserAllowedClient.user_id == user_id)
        )
        for c in clients:
            self._session.add(
                UserAllowedClient(user_id=user_id, client=c, created_by=created_by)
            )
        await self._session.flush()

    async def clear_for_user(self, user_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(UserAllowedClient).where(UserAllowedClient.user_id == user_id)
        )
        await self._session.flush()
