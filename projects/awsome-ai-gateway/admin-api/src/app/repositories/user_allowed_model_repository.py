# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model import UserAllowedModel


class UserAllowedModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_user(self, user_id: uuid.UUID) -> list[str]:
        stmt = select(UserAllowedModel.model_alias).where(
            UserAllowedModel.user_id == user_id
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def replace_for_user(
        self, user_id: uuid.UUID, aliases: list[str], created_by: uuid.UUID
    ) -> None:
        # replace-all: 0개면 모두 삭제 = override 해제(팀 정책으로 폴백).
        # ★ team_allowed_models(0행=전체허용)와 의미가 다르다 — 여기 0행은 "팀 폴백".
        # 그 차이는 스냅샷 시점(key_service / auth_service)의 user>team>none 분기가 책임진다.
        await self._session.execute(
            delete(UserAllowedModel).where(UserAllowedModel.user_id == user_id)
        )
        for a in aliases:
            self._session.add(
                UserAllowedModel(user_id=user_id, model_alias=a, created_by=created_by)
            )
        await self._session.flush()

    async def clear_for_user(self, user_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(UserAllowedModel).where(UserAllowedModel.user_id == user_id)
        )
        await self._session.flush()
