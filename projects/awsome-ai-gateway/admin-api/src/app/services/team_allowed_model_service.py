# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import NotFoundError, ValidationError
from app.repositories.model_repository import ModelRepository, TeamAllowedModelRepository
from app.repositories.user_repository import UserRepository
from app.schemas.models import AllowedModelsResponse

logger = structlog.get_logger()


class TeamAllowedModelService:
    """FR-2.6: 팀별 모델 접근 제어.

    엔트리 0개 → 전체 허용 (하위 호환).
    엔트리 존재 → 화이트리스트 enforcement.
    """

    def __init__(self, cache_mgr: CacheInvalidationManager) -> None:
        self._cache_mgr = cache_mgr

    async def list_for_team(
        self, session: AsyncSession, *, team_id: uuid.UUID
    ) -> AllowedModelsResponse:
        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        repo = TeamAllowedModelRepository(session)
        aliases = await repo.list_by_team(team_id)
        return AllowedModelsResponse(team_id=str(team_id), model_aliases=aliases)

    async def set_for_team(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        model_aliases: list[str],
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> AllowedModelsResponse:
        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        # Validate aliases exist and are ACTIVE
        model_repo = ModelRepository(session)
        for alias in set(model_aliases):
            m = await model_repo.get_by_alias(alias)
            if m is None:
                raise ValidationError(f"Unknown model alias: {alias}")

        repo = TeamAllowedModelRepository(session)
        before = await repo.list_by_team(team_id)
        after = await repo.set_for_team(team_id, model_aliases, actor.user_id)

        await self._invalidate_team_vk_cache(session, team_id)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_TEAM_ALLOWED_MODELS",
            resource_type="TeamAllowedModels",
            resource_id=str(team_id),
            changes={"before": before, "after": after},
            ip_address=ip_address,
            request_id=request_id,
        )

        return AllowedModelsResponse(team_id=str(team_id), model_aliases=after)

    async def clear_for_team(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> AllowedModelsResponse:
        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        repo = TeamAllowedModelRepository(session)
        before = await repo.list_by_team(team_id)
        await repo.clear_for_team(team_id)

        await self._invalidate_team_vk_cache(session, team_id)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CLEAR_TEAM_ALLOWED_MODELS",
            resource_type="TeamAllowedModels",
            resource_id=str(team_id),
            changes={"before": before, "after": []},
            ip_address=ip_address,
            request_id=request_id,
        )

        return AllowedModelsResponse(team_id=str(team_id), model_aliases=[])

    async def _invalidate_team_vk_cache(
        self, session: AsyncSession, team_id: uuid.UUID
    ) -> None:
        """팀 소속 사용자의 AuthContext 캐시(`key:cache:vk:*`, `user_context:*`) 무효화.

        VK의 raw key 해시를 모르므로 `key:cache:vk:*`는 발급 시 저장한 reverse index
        `team:vk_hashes:{team_id}` 를 통해 DEL. reverse index 미존재 시 TTL(300s)
        로 자연 만료 대기.
        """
        from sqlalchemy import select

        from app.models.auth import User

        stmt = select(User.id).where(User.team_id == team_id)
        result = await session.execute(stmt)
        user_ids = [str(uid) for uid in result.scalars().all()]
        user_keys = [f"user_context:{uid}" for uid in user_ids]

        # VK hash reverse index (populated at issue time)
        try:
            vk_hashes = await self._cache_mgr._redis.smembers(
                f"team:vk_hashes:{team_id}"
            )
            vk_keys = [
                f"key:cache:vk:{h.decode() if isinstance(h, bytes) else h}"
                for h in vk_hashes
            ]
        except Exception:
            vk_keys = []
            logger.warning("team_allowed_models.vk_index_read_failed", team_id=str(team_id), exc_info=True)

        all_keys = user_keys + vk_keys
        if all_keys:
            await self._cache_mgr.invalidate(all_keys, session=session)
