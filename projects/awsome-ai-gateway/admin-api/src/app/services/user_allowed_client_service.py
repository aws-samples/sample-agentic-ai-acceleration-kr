# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import uuid

import structlog

from app.repositories.user_allowed_client_repository import UserAllowedClientRepository
from app.repositories.user_repository import UserRepository

logger = structlog.get_logger(__name__)

_VALID_CLIENTS = {"claude-code", "cowork", "codex"}


class UserAllowedClientService:
    def __init__(self, session, cache_mgr=None, key_service=None) -> None:
        self._session = session
        self._cache_mgr = cache_mgr  # optional: to invalidate VK auth cache for the user
        self._key_service = key_service  # optional: to resolve the user's active VK hashes

    async def get(self, user_id: uuid.UUID) -> list[str]:
        return await UserAllowedClientRepository(self._session).list_by_user(user_id)

    async def set(self, user_id: uuid.UUID, clients: list[str], admin_id: uuid.UUID) -> list[str]:
        invalid = [c for c in clients if c not in _VALID_CLIENTS]
        if invalid:
            raise ValueError(f"invalid clients: {invalid}; allowed={sorted(_VALID_CLIENTS)}")
        # dedup, preserve canonical set
        clients = sorted(set(clients))
        repo = UserAllowedClientRepository(self._session)
        user = await UserRepository(self._session).get_user(user_id)
        if user is None:
            raise LookupError(f"user not found: {user_id}")
        await repo.replace_for_user(user_id, clients, admin_id)
        await self._invalidate(user_id)
        logger.info(
            "admin.set_user_allowed_clients",
            user_id=str(user_id), clients=clients, admin_id=str(admin_id),
        )
        return clients

    async def clear(self, user_id: uuid.UUID, admin_id: uuid.UUID) -> None:
        await UserAllowedClientRepository(self._session).clear_for_user(user_id)
        await self._invalidate(user_id)
        logger.info(
            "admin.clear_user_allowed_clients",
            user_id=str(user_id), admin_id=str(admin_id),
        )

    async def _invalidate(self, user_id: uuid.UUID) -> None:
        # 변경 즉시 반영: 해당 user 의 ACTIVE VK 들의 auth 캐시(`key:cache:vk:*`)를 DEL.
        # cache_mgr/key_service 미주입 시(read-only 경로 등) no-op → VK 캐시 TTL(~300s)로 자연 만료.
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
                "allowed_clients_cache_invalidate_failed",
                user_id=str(user_id),
                exc_info=True,
            )
