# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""RoutingProfileService — read/update per-client web_search_enabled toggle.

Writes model.routing_profiles.web_search_enabled, then DELs the gateway's Redis
cache key `routing_profile:{client}` (invalidate-only, like ModelService) so the
gateway repopulates from DB on the next request. Same ElastiCache as gateway-proxy.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select

from app.models.routing import RoutingProfile

logger = structlog.get_logger(__name__)

# Must match gateway-proxy client_identifier tokens + user_allowed_client_service.
_VALID_CLIENTS = {"claude-code", "cowork", "codex"}
# Cache key written by gateway-proxy RoutingProfileLoader (routing_profile_loader.py).
_CACHE_KEY = "routing_profile:{client}"


class RoutingProfileService:
    def __init__(self, session, cache_mgr=None) -> None:
        self._session = session
        self._cache_mgr = cache_mgr

    async def list_profiles(self) -> list[dict]:
        """All routing profiles with their web_search flag (for the admin toggle UI)."""
        rows = (await self._session.execute(select(RoutingProfile))).scalars().all()
        return [
            {
                "client": r.client,
                "web_search_enabled": bool(r.web_search_enabled),
                "backend": r.backend,
                "enabled": bool(r.enabled),
            }
            for r in sorted(rows, key=lambda x: x.client)
        ]

    async def set_web_search(self, client: str, enabled: bool, admin_id) -> dict:
        if client not in _VALID_CLIENTS:
            raise ValueError(f"invalid client: {client}; allowed={sorted(_VALID_CLIENTS)}")
        row = (
            await self._session.execute(
                select(RoutingProfile).where(RoutingProfile.client == client)
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(f"routing profile not found for client: {client}")
        row.web_search_enabled = bool(enabled)
        await self._session.flush()
        await self._invalidate(client)
        logger.info(
            "admin.set_web_search_enabled",
            client=client, web_search_enabled=bool(enabled), admin_id=str(admin_id),
        )
        return {"client": client, "web_search_enabled": bool(enabled)}

    async def _invalidate(self, client: str) -> None:
        """DEL routing_profile:{client} so gateway-proxy repopulates from DB (self-heal)."""
        if self._cache_mgr is None:
            return  # no-op → gateway's 300s TTL expires naturally
        try:
            await self._cache_mgr.invalidate(
                [_CACHE_KEY.format(client=client)], session=self._session
            )
        except Exception:
            logger.warning(
                "routing_profile_cache_invalidate_failed", client=client, exc_info=True
            )
