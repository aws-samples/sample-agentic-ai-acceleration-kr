# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import json
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.routing import RoutingProfile
from app.schemas.routing import RoutingProfileSchema

logger = structlog.get_logger(__name__)

ROUTING_CACHE_TTL = 300  # 5 min, mirrors MODEL_CACHE_TTL


class RoutingProfileLoader:
    """Loads model.routing_profiles rows by client, Redis-cached.

    Returns None when there is no enabled profile for the client -> callers
    fall back to the default (Claude Code / Bedrock InvokeModel) path.
    """

    async def load(
        self, redis, db: Optional[AsyncSession], client: Optional[str]
    ) -> Optional[RoutingProfileSchema]:
        if not client:
            return None

        cache_key = f"routing_profile:{client}"
        if redis is not None:
            try:
                cached = await redis.get(cache_key)
            except Exception:
                cached = None
            if cached:
                try:
                    schema = RoutingProfileSchema(**json.loads(cached))
                    return schema if schema.enabled else None
                except Exception:
                    logger.warning("routing_profile_cache_parse_failed", client=client)

        if db is None:
            return None

        result = await db.execute(
            select(RoutingProfile).where(RoutingProfile.client == client)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None

        schema = RoutingProfileSchema(
            client=row.client,
            backend=row.backend,
            account_role_arn=row.account_role_arn,
            region=row.region,
            default_model=row.default_model,
            external_id=row.external_id,
            enabled=row.enabled,
            web_search_enabled=row.web_search_enabled,
        )
        if redis is not None:
            try:
                await redis.setex(
                    cache_key, ROUTING_CACHE_TTL, json.dumps(schema.model_dump(mode="json"))
                )
            except Exception:
                logger.warning("routing_profile_cache_write_failed", client=client)

        return schema if schema.enabled else None
