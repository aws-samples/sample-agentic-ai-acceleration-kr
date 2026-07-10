# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster

from worker.config import Settings

logger = structlog.get_logger(__name__)

# XREADGROUP 전용 연결 + 보조 명령(XACK/XADD 재시도/PUBLISH 등)용 여유분
_REDIS_MAX_CONNECTIONS = 20


async def create_redis_client(settings: Settings) -> Redis | RedisCluster:
    """standalone/cluster 자동 감지 Redis 클라이언트."""
    kwargs: dict = {"max_connections": _REDIS_MAX_CONNECTIONS}
    if settings.redis_tls_enabled:
        kwargs["ssl"] = True

    probe = Redis.from_url(settings.redis_url)
    try:
        info = await probe.info("server")
        mode = info.get("redis_mode", "standalone")
    except Exception:
        mode = "standalone"
    finally:
        await probe.aclose()

    logger.info("redis_mode_detected", mode=mode, url=settings.redis_url)

    if mode == "cluster":
        return RedisCluster.from_url(settings.redis_url, **kwargs)
    return Redis.from_url(settings.redis_url, **kwargs)
