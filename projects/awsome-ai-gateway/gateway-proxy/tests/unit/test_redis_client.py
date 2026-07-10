# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""redis_client 연결 복원력 검증 (deepdive Q50 Phase 2).

과거엔 max_connections 만 전달해 socket_timeout=None(무한 블로킹)이었다 — 느린
노드 하나가 풀을 전 pod 에서 고갈. 아래 테스트는 타임아웃/재시도/health-check 가
실제 연결 kwargs 로 들어가는지, 0 으로 끄면 과거 동작(미설정)으로 돌아가는지 잠근다.
"""

from __future__ import annotations

from app.config import Settings
from app.redis_client import _resilience_kwargs


def test_resilience_defaults_set_timeouts_and_retry():
    kw = _resilience_kwargs(Settings())
    # 기본값(부하 강건성 ON): socket/connect 타임아웃 + retry + health-check 존재.
    assert kw["socket_timeout"] == 2.0
    assert kw["socket_connect_timeout"] == 1.0
    assert kw["health_check_interval"] == 30.0
    assert "retry" in kw
    assert "retry_on_error" in kw
    assert kw["max_connections"] == Settings().redis_pool_size


def test_resilience_zero_disables_each_lever():
    """0 이면 해당 항목 생략 → 과거 동작(미설정) 복귀(안전밸브)."""
    s = Settings(
        redis_socket_timeout=0,
        redis_connect_timeout=0,
        redis_retries=0,
        redis_health_check_interval=0,
    )
    kw = _resilience_kwargs(s)
    assert "socket_timeout" not in kw
    assert "socket_connect_timeout" not in kw
    assert "health_check_interval" not in kw
    assert "retry" not in kw
    assert "retry_on_error" not in kw
    # max_connections 는 항상 유지(과거 유일 kwarg).
    assert kw["max_connections"] == s.redis_pool_size


def test_resilience_retry_only_on_timeout_and_connection_errors():
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    kw = _resilience_kwargs(Settings(redis_retries=2))
    assert set(kw["retry_on_error"]) == {RedisTimeoutError, RedisConnectionError}


def test_standalone_client_pool_carries_socket_timeout():
    """from_url 빌드 시 socket_timeout 이 connection pool 까지 전달되는지 (I/O 없음)."""
    from redis.asyncio import Redis

    kw = _resilience_kwargs(Settings(redis_socket_timeout=2.5))
    r = Redis.from_url("redis://localhost:6379/0", **kw)
    assert r.connection_pool.connection_kwargs.get("socket_timeout") == 2.5
