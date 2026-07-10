# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog
from redis.asyncio import Redis

from worker.config import Settings

logger = structlog.get_logger(__name__)

# Pub/Sub worker: 5채널 × 전용연결 + health_check + config_reload + 여유분
_REDIS_MAX_CONNECTIONS = 20


async def create_redis_client(settings: Settings) -> Redis:
    """Redis 클라이언트 생성 (Pub/Sub 전용 워커).

    redis-py async `RedisCluster` 는 7.x 까지도 pub/sub 미지원이므로
    cluster mode 환경에서도 standalone `Redis` 클라이언트를 반환한다.
    ElastiCache cluster 의 non-sharded PUBLISH 는 모든 노드에 broadcast 되므로
    단일 노드 연결로 모든 메시지 수신이 가능. cluster 감지 로직은 INFO 진단용.
    """
    kwargs: dict = {
        "max_connections": _REDIS_MAX_CONNECTIONS,
    }

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

    return Redis.from_url(settings.redis_url, **kwargs)


# Module singleton
_client: Redis | None = None


def set_redis_client(client: Redis) -> None:
    global _client
    _client = client


def get_redis_client() -> Redis:
    assert _client is not None, "Redis client not initialised. Call set_redis_client() first."
    return _client
