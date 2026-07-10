# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import structlog
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import Settings

logger = structlog.get_logger(__name__)


def _resilience_kwargs(settings: Settings) -> dict:
    """연결 복원력 kwargs (deepdive Q50 Phase 2).

    socket_timeout 이 없으면(과거) 느린 노드가 awaited 호출을 무한 블로킹해 풀을
    전 pod 에서 고갈시킨다. 명령/연결 타임아웃 + blip 재시도 + idle health-check 로
    "느린 Redis → 빠른 실패 → 상위 fallback" 으로 바꾼다. 값 0/음수면 해당 항목 생략
    (과거 동작 복귀 — 안전밸브). hot-path 라 값 조정 시 load A/B 권장.
    """
    kwargs: dict = {"max_connections": settings.redis_pool_size}

    if settings.redis_socket_timeout and settings.redis_socket_timeout > 0:
        kwargs["socket_timeout"] = settings.redis_socket_timeout
    if settings.redis_connect_timeout and settings.redis_connect_timeout > 0:
        kwargs["socket_connect_timeout"] = settings.redis_connect_timeout
    if settings.redis_health_check_interval and settings.redis_health_check_interval > 0:
        kwargs["health_check_interval"] = settings.redis_health_check_interval

    if settings.redis_retries and settings.redis_retries > 0:
        # 단발 timeout/connection blip 만 백오프 재시도(50ms→cap 500ms). 느린 DB
        # 강등 전 흡수. 다른 예외(논리 오류 등)는 재시도 안 함.
        kwargs["retry"] = Retry(ExponentialBackoff(cap=0.5, base=0.05), settings.redis_retries)
        kwargs["retry_on_error"] = [RedisTimeoutError, RedisConnectionError]

    return kwargs


def _cluster_kwargs(settings: Settings) -> dict:
    """cluster 전용 추가 kwargs(deepdive Q50 Phase4-f). read_from_replicas 는
    RedisCluster 에만 유효(standalone 엔 전달 금지)."""
    kw = _resilience_kwargs(settings)
    if settings.redis_read_from_replicas:
        kw["read_from_replicas"] = True
    return kw


async def create_redis_client(settings: Settings) -> Redis | RedisCluster:
    """Redis 클라이언트 생성. REDIS_CLUSTER_MODE 환경변수로 모드 결정.
    미설정 시 INFO 명령으로 자동 감지 (Startup Probe).
    """
    kwargs = _resilience_kwargs(settings)

    if settings.redis_cluster_mode is True:
        logger.info("redis_mode", mode="cluster", url=settings.redis_url)
        return RedisCluster.from_url(settings.redis_url, **_cluster_kwargs(settings))

    if settings.redis_cluster_mode is False:
        logger.info("redis_mode", mode="standalone", url=settings.redis_url)
        return Redis.from_url(settings.redis_url, **kwargs)

    # Auto-detect via Startup Probe — 프로브에도 connect/socket 타임아웃을 줘야
    # 느린 노드/TLS 핸드셰이크에서 pod 부팅이 무한 대기하지 않는다(과거엔 무타임아웃).
    # 끄기(0)로 설정해도 프로브만은 2s/1s 하한을 둬 부팅 무한대기를 막는다.
    probe = Redis.from_url(
        settings.redis_url,
        socket_timeout=(settings.redis_socket_timeout or 2.0),
        socket_connect_timeout=(settings.redis_connect_timeout or 1.0),
    )
    try:
        info = await probe.info("server")
        mode = info.get("redis_mode", "standalone")
    except Exception:
        mode = "standalone"
    finally:
        await probe.aclose()

    logger.info("redis_mode_autodetect", mode=mode, url=settings.redis_url)

    if mode == "cluster":
        return RedisCluster.from_url(settings.redis_url, **_cluster_kwargs(settings))

    return Redis.from_url(settings.redis_url, **kwargs)
