# Copyright 2026 © Amazon.com and Affiliates.
"""Per-user model allow-list (overrides team_allowed_models).

Precedence (resolved at AuthContext-snapshot time, NOT here): user > team > none.
This service only owns the user-scoped rows + cache invalidation. Mirrors
UserAllowedClientService, plus alias existence/ACTIVE validation borrowed from
TeamAllowedModelService.set_for_team.
"""
from __future__ import annotations

import uuid

import structlog

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.models.model import ModelStatus
from app.repositories.model_repository import ModelRepository
from app.repositories.user_allowed_model_repository import UserAllowedModelRepository
from app.repositories.user_repository import UserRepository

logger = structlog.get_logger(__name__)


class UserAllowedModelService:
    def __init__(self, session, cache_mgr=None, key_service=None) -> None:
        self._session = session
        self._cache_mgr = cache_mgr  # optional: invalidate VK auth cache for the user
        self._key_service = key_service  # optional: resolve the user's active VK hashes

    async def get(self, user_id: uuid.UUID) -> list[str]:
        return await UserAllowedModelRepository(self._session).list_by_user(user_id)

    async def set(
        self,
        user_id: uuid.UUID,
        aliases: list[str],
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> list[str]:
        # dedup, stable order
        aliases = sorted(set(aliases))
        if not aliases:
            # Explicit empty set via PUT is ambiguous; treat as "clear override"
            # (fall back to team) to match the DELETE semantics. Reject here so the
            # caller uses DELETE intentionally rather than silently clearing.
            raise ValueError(
                "empty model list: use DELETE /allowed-models to clear the per-user "
                "override (falls back to team policy)"
            )

        repo = UserAllowedModelRepository(self._session)
        user = await UserRepository(self._session).get_user(user_id)
        if user is None:
            raise LookupError(f"user not found: {user_id}")

        # Validate each alias exists AND is ACTIVE (mirror TeamAllowedModelService).
        # Restricting a national-core-tech user to an INACTIVE/unknown alias would
        # silently deny everything — reject loudly instead.
        model_repo = ModelRepository(self._session)
        for alias in aliases:
            m = await model_repo.get_by_alias(alias)
            if m is None:
                raise ValueError(f"unknown model alias: {alias}")
            if m.status != ModelStatus.ACTIVE:
                raise ValueError(f"model alias is not ACTIVE: {alias}")

        before = await repo.list_by_user(user_id)
        await repo.replace_for_user(user_id, aliases, actor.user_id)

        # Durable audit (Codex MF2) — national-core-tech control needs a permanent
        # record, like SET_TEAM_ALLOWED_MODELS. logger.info alone is not durable.
        await audit_logger.log(
            self._session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_USER_ALLOWED_MODELS",
            resource_type="UserAllowedModels",
            resource_id=str(user_id),
            changes={"before": sorted(before), "after": aliases},
            ip_address=ip_address,
            request_id=request_id,
        )
        return aliases

    async def clear(
        self,
        user_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        repo = UserAllowedModelRepository(self._session)
        before = await repo.list_by_user(user_id)
        await repo.clear_for_user(user_id)
        await audit_logger.log(
            self._session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CLEAR_USER_ALLOWED_MODELS",
            resource_type="UserAllowedModels",
            resource_id=str(user_id),
            changes={"before": sorted(before), "after": []},
            ip_address=ip_address,
            request_id=request_id,
        )

    async def invalidate_user_vk_cache(self, user_id: uuid.UUID) -> None:
        # 변경 즉시 반영: 해당 user 의 ACTIVE VK auth 캐시(`key:cache:vk:*`)를 DEL.
        # ★ Codex MF3: 라우트가 session.commit() 이후에 호출한다 — commit 전에 지우면
        #   delete→commit 윈도에 게이트웨이 요청이 옛 정책으로 캐시를 재생성할 수 있다.
        # cache_mgr/key_service 미주입 시 no-op → VK 캐시 TTL(~300s)로 자연 만료.
        if self._cache_mgr is None or self._key_service is None:
            return
        try:
            hashes = await self._key_service.list_active_vk_hashes_for_user(
                self._session, user_id
            )
            keys = [f"key:cache:vk:{h}" for h in hashes]
            if keys:
                await self._cache_mgr.invalidate(keys, session=self._session)
        except Exception:
            logger.warning(
                "allowed_models_cache_invalidate_failed",
                user_id=str(user_id),
                exc_info=True,
            )
