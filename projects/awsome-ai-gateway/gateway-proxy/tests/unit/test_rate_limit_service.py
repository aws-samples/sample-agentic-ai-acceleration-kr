# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.rate_limit_scope import (
    RateLimitScope,
    ScopeDescriptor,
    build_rl_key,
    build_scope_descriptors,
    compute_tpm_incr,
    estimate_reserved_tokens,
)
from app.services.rate_limit_service import InMemoryRateLimiter, RateLimitService


@pytest.mark.asyncio
async def test_check_rpm_allowed(mock_redis):
    mock_redis.eval = AsyncMock(
        return_value=json.dumps(
            {"allowed": True, "remaining": 59, "limit": 60, "retry_after": None, "window_reset": 0}
        ).encode()
    )
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"

    svc = RateLimitService()
    result = await svc.check_rpm(mock_redis, "user-1", "model-1", 60)
    assert result.allowed is True
    assert result.remaining == 59


@pytest.mark.asyncio
async def test_check_rpm_exceeded(mock_redis):
    mock_redis.eval = AsyncMock(
        return_value=json.dumps(
            {"allowed": False, "remaining": 0, "limit": 60, "retry_after": 30, "window_reset": 0}
        ).encode()
    )
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"

    svc = RateLimitService()
    result = await svc.check_rpm(mock_redis, "user-1", "model-1", 60)
    assert result.allowed is False
    assert result.retry_after == 30


def test_in_memory_rate_limiter_allowed():
    limiter = InMemoryRateLimiter(worker_count=1)
    result = limiter.check("key-1", limit=60)
    assert result.allowed is True


def test_in_memory_rate_limiter_exceeded():
    limiter = InMemoryRateLimiter(worker_count=1)
    for _ in range(60):
        limiter.check("key-1", limit=60)
    result = limiter.check("key-1", limit=60)
    assert result.allowed is False


def test_in_memory_worker_split():
    limiter = InMemoryRateLimiter(worker_count=4)
    # 4 워커 -> 워커당 한도 = 60 // 4 = 15
    for _ in range(15):
        limiter.check("key-1", limit=60)
    result = limiter.check("key-1", limit=60)
    assert result.allowed is False


def test_fallback_divisor_default_preserves_legacy_behavior():
    """rl_fallback_replicas 기본 1 → divisor = 1 × uvicorn_workers(=과거 동작 보존).

    과거 미들웨어는 worker_count=4 를 하드코딩했다(uvicorn_workers 가정·HPA replica
    무시). 기본값에서 divisor 가 uvicorn_workers 와 동일함을 잠근다(무행동변경 머지).
    """
    from app.config import Settings

    s = Settings(rl_fallback_replicas=1, uvicorn_workers=4)
    divisor = max(1, s.rl_fallback_replicas) * max(1, s.uvicorn_workers)
    assert divisor == 4  # 과거 하드코딩 4 와 동일


def test_fallback_divisor_scales_with_replicas():
    """rl_fallback_replicas 를 올리면 divisor 가 replicas × workers 로 커진다.

    HPA replica 30 × uvicorn 4 = 120 분할 → 한도 600 이면 카운터당 5. 부하테스트
    429×6 의 원인(고정 4 가 replica 무시)을 직접 닫는 경로(deepdive Q46/Q50).
    """
    from app.config import Settings

    s = Settings(rl_fallback_replicas=30, uvicorn_workers=4)
    divisor = max(1, s.rl_fallback_replicas) * max(1, s.uvicorn_workers)
    assert divisor == 120

    limiter = InMemoryRateLimiter(worker_count=divisor)
    # 카운터당 한도 = 600 // 120 = 5
    for _ in range(5):
        limiter.check("k", limit=600)
    assert limiter.check("k", limit=600).allowed is False


# ── FR-4.1~4.5 멀티 스코프 테스트 ──


def test_build_rl_key_user_scope():
    key = build_rl_key(RateLimitScope.USER, "user-1", "claude-opus", "rpm")
    # 단일 중괄호 hash tag (Redis Cluster 슬롯 라우팅용)
    assert key == "{USER:user-1:claude-opus}:rpm"


def test_build_rl_key_global_wildcard():
    # GLOBAL 스코프는 scope_id None → '*' 대체
    key = build_rl_key(RateLimitScope.GLOBAL, None, "claude-opus", "rpm")
    assert key == "{GLOBAL:*:claude-opus}:rpm"


def test_build_rl_key_hash_tag_shared_across_metrics():
    # 같은 scope+id+model 의 서로 다른 metric은 동일 hash tag → 동일 클러스터 슬롯
    from app.services.rate_limit_scope import build_tpm_key_group

    rpm_key = build_rl_key(RateLimitScope.USER, "u1", "m1", "rpm")
    cur, prev, window = build_tpm_key_group(RateLimitScope.USER, "u1", "m1")

    def hash_tag(key: str) -> str:
        start = key.index("{") + 1
        end = key.index("}")
        return key[start:end]

    tag = hash_tag(rpm_key)
    assert hash_tag(cur) == tag
    assert hash_tag(prev) == tag
    assert hash_tag(window) == tag


def test_build_scope_descriptors_with_team():
    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=60,
        user_tpm=10000,
        team_rpm=600,
        team_tpm=100000,
        global_rpm=10000,
        global_tpm=1000000,
    )
    # USER → TEAM → GLOBAL fast-fail 순서
    assert [d.scope for d in descriptors] == [
        RateLimitScope.USER,
        RateLimitScope.TEAM,
        RateLimitScope.GLOBAL,
    ]


def test_build_scope_descriptors_without_team():
    # team_id None 이면 TEAM 스코프 건너뜀
    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id=None,
        model_alias="m1",
        user_rpm=60,
        user_tpm=None,
        team_rpm=600,
        team_tpm=None,
        global_rpm=10000,
        global_tpm=None,
    )
    assert [d.scope for d in descriptors] == [
        RateLimitScope.USER,
        RateLimitScope.GLOBAL,
    ]


@pytest.mark.asyncio
async def test_check_multi_scope_rpm_allowed(mock_redis):
    mock_redis.eval = AsyncMock(
        return_value=json.dumps(
            {
                "allowed": True,
                "scope": None,
                "limit_type": None,
                "limit": -1,
                "remaining": -1,
                "retry_after": None,
                "window_reset": None,
            }
        ).encode()
    )
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=60,
        user_tpm=None,
        team_rpm=600,
        team_tpm=None,
        global_rpm=10000,
        global_tpm=None,
    )

    svc = RateLimitService()
    result = await svc.check_multi_scope_rpm(mock_redis, descriptors)

    assert result.allowed is True
    assert result.scope is None
    # CROSSSLOT 대응(deepdive Q50): scope 별 1키씩 분리 호출 → eval 3회, 각 numkeys=1.
    assert mock_redis.eval.await_count == 3
    for call in mock_redis.eval.await_args_list:
        assert call.args[1] == 1


@pytest.mark.asyncio
async def test_check_multi_scope_rpm_user_violation(mock_redis):
    # USER 스코프에서 거부되는 시나리오 — fast-fail 로 여기서 종료
    mock_redis.eval = AsyncMock(
        return_value=json.dumps(
            {
                "allowed": False,
                "scope": "USER",
                "limit_type": "rpm",
                "limit": 60,
                "remaining": 0,
                "retry_after": 42,
                "window_reset": 1713574000,
            }
        ).encode()
    )
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=60,
        user_tpm=None,
        team_rpm=600,
        team_tpm=None,
        global_rpm=10000,
        global_tpm=None,
    )

    svc = RateLimitService()
    result = await svc.check_multi_scope_rpm(mock_redis, descriptors)

    assert result.allowed is False
    assert result.scope == "USER"
    assert result.limit_type == "rpm"
    assert result.retry_after == 42


@pytest.mark.asyncio
async def test_check_multi_scope_rpm_all_unlimited(mock_redis):
    # 모든 한도 None/0 이면 Lua 호출 없이 통과
    descriptors = [
        ScopeDescriptor(
            scope=RateLimitScope.USER, scope_id="u1", model_alias="m1", rpm_limit=None
        ),
        ScopeDescriptor(
            scope=RateLimitScope.GLOBAL, scope_id=None, model_alias="m1", rpm_limit=0
        ),
    ]

    svc = RateLimitService()
    result = await svc.check_multi_scope_rpm(mock_redis, descriptors)

    assert result.allowed is True
    # Lua 호출 안 됐는지 확인
    mock_redis.eval.assert_not_called()


@pytest.mark.asyncio
async def test_check_multi_scope_rpm_fail_open_on_redis_error(mock_redis):
    # Redis 에러 → fail-open (NFR-2.4)
    mock_redis.eval = AsyncMock(side_effect=Exception("redis down"))
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=60,
        user_tpm=None,
        team_rpm=600,
        team_tpm=None,
        global_rpm=10000,
        global_tpm=None,
    )

    svc = RateLimitService()
    result = await svc.check_multi_scope_rpm(mock_redis, descriptors)

    # Fail-open: 요청 통과
    assert result.allowed is True


@pytest.mark.asyncio
async def test_fail_mode_closed_blocks_on_eval_error(mock_redis, monkeypatch):
    """rl_fail_mode='closed' 시 eval 예외가 fail-open(통과) 아닌 차단(allowed=False).

    기본 'open' 은 기존 fail-open 테스트가 커버. 여기선 명시 정책 전환만 검증
    (deepdive Q50 — 무단 flip 금지, 설정으로만).
    """
    from app.config import get_settings
    from app.services import rate_limit_service as rls
    from app.services.lua_loader import LuaScriptLoader

    get_settings.cache_clear()
    monkeypatch.setenv("RL_FAIL_MODE", "closed")
    rls.reset_breaker_for_test()
    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"
    mock_redis.eval = AsyncMock(side_effect=Exception("redis down"))

    desc = [
        ScopeDescriptor(scope=RateLimitScope.USER, scope_id="u1", model_alias="m1", rpm_limit=60),
    ]
    try:
        result = await rls.RateLimitService().check_multi_scope_rpm(mock_redis, desc)
        assert result.allowed is False  # fail-CLOSED
        assert result.scope == "USER"
    finally:
        get_settings.cache_clear()  # 다른 테스트 오염 방지
        rls.reset_breaker_for_test()


@pytest.mark.asyncio
async def test_circuit_breaker_fast_fails_after_threshold(mock_redis):
    """eval 연속 실패가 임계 넘으면 회로 OPEN → 이후 eval 호출 안 하고 fast-fail(fail-open).

    deepdive Q50: Redis 죽었을 때 매 요청 socket_timeout 대기 누적 방지.
    """
    from app.services import rate_limit_service as rls
    from app.services.lua_loader import LuaScriptLoader

    rls.reset_breaker_for_test()  # 깨끗한 breaker(기본 fail_threshold=5)
    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"
    mock_redis.eval = AsyncMock(side_effect=Exception("redis down"))

    # USER 한 scope만 — scope당 1 eval. fail_threshold(5)를 넘기려면 5+ 요청.
    desc = [
        ScopeDescriptor(scope=RateLimitScope.USER, scope_id="u1", model_alias="m1", rpm_limit=60),
    ]
    svc = rls.RateLimitService()
    for _ in range(5):
        r = await svc.check_multi_scope_rpm(mock_redis, desc)
        assert r.allowed is True  # 매번 fail-open

    eval_calls_before = mock_redis.eval.await_count
    # 회로가 열렸으니 다음 호출은 eval 을 더 부르지 않아야 한다(fast-fail).
    r = await svc.check_multi_scope_rpm(mock_redis, desc)
    assert r.allowed is True  # 여전히 fail-open(통과)
    assert mock_redis.eval.await_count == eval_calls_before  # eval 추가 호출 0
    rls.reset_breaker_for_test()


@pytest.mark.asyncio
async def test_fail_open_increments_metric(mock_redis):
    """fail-open(eval 예외) 시 관측 카운터가 scope당 증가하는지(무음 아님, Q50 Phase 3)."""
    from unittest.mock import MagicMock

    from app.services import rate_limit_service as rls
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"
    mock_redis.eval = AsyncMock(side_effect=Exception("redis down"))

    counter = MagicMock()
    rls.set_fail_open_metric(counter)
    try:
        descriptors = build_scope_descriptors(
            user_id="u1", team_id="t1", model_alias="m1",
            user_rpm=60, user_tpm=None, team_rpm=600, team_tpm=None,
            global_rpm=10000, global_tpm=None,
        )
        result = await rls.RateLimitService().check_multi_scope_rpm(mock_redis, descriptors)
        assert result.allowed is True  # fail-open
        # 3 scope 모두 eval 예외 → 3회 카운트, scope/limit_type 라벨 부착.
        assert counter.add.call_count == 3
        for call in counter.add.call_args_list:
            assert call.args[0] == 1
            assert call.args[1]["limit_type"] == "rpm"
    finally:
        rls.set_fail_open_metric(None)  # 다른 테스트 오염 방지


# ── FR-4.1 D3 TPM 공식 + Counter 테스트 ──


def test_compute_tpm_incr_excludes_cache_read():
    """D3 공식: input + cache_creation + output (cache_read 제외)."""

    class _Usage:
        input_tokens = 1000
        output_tokens = 500
        cache_creation_input_tokens = 200
        cache_read_input_tokens = 3000  # 제외 대상

    assert compute_tpm_incr(_Usage()) == 1700  # 1000 + 200 + 500


def test_compute_tpm_incr_no_cache():
    """캐시 미사용 시 input + output만 집계."""

    class _Usage:
        input_tokens = 100
        output_tokens = 50
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0

    assert compute_tpm_incr(_Usage()) == 150


def test_estimate_reserved_tokens_upper_bound():
    """Pre-reserve는 max_output을 상한으로 보수적으로 예약."""
    reserved = estimate_reserved_tokens(
        estimated_input_tokens=1000,
        max_output_tokens=4096,
        estimated_cache_creation_tokens=200,
    )
    assert reserved == 5296


@pytest.mark.asyncio
async def test_check_multi_scope_tpm_allowed(mock_redis):
    mock_redis.eval = AsyncMock(
        return_value=json.dumps(
            {
                "allowed": True,
                "scope": None,
                "limit_type": None,
                "limit": -1,
                "remaining": -1,
                "retry_after": None,
                "window_reset": None,
            }
        ).encode()
    )
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_tpm_check"] = "-- mock"

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=None,
        user_tpm=10000,
        team_rpm=None,
        team_tpm=100000,
        global_rpm=None,
        global_tpm=1000000,
    )

    svc = RateLimitService()
    result = await svc.check_multi_scope_tpm(
        mock_redis, descriptors, reserved_tokens=5000
    )
    assert result.allowed is True
    # CROSSSLOT 대응(deepdive Q50): scope 별 분리 호출 → eval 3회, 각 numkeys=4
    # (한 scope 의 cur/prev/window 와 예약 표지는 동일 hash tag = 단일 슬롯).
    assert mock_redis.eval.await_count == 3
    for call in mock_redis.eval.await_args_list:
        assert call.args[1] == 4
        cur, marker = call.args[2], call.args[5]
        assert marker.startswith(cur.replace(":tpm:cur", ":tpm-reserved:"))


@pytest.mark.asyncio
async def test_check_multi_scope_tpm_tpm_exceeded(mock_redis):
    mock_redis.eval = AsyncMock(
        return_value=json.dumps(
            {
                "allowed": False,
                "scope": "TEAM",
                "limit_type": "tpm",
                "limit": 100000,
                "remaining": 0,
                "retry_after": 30,
                "window_reset": 1713574000,
            }
        ).encode()
    )
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_tpm_check"] = "-- mock"

    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=None,
        user_tpm=10000,
        team_rpm=None,
        team_tpm=100000,
        global_rpm=None,
        global_tpm=1000000,
    )

    svc = RateLimitService()
    result = await svc.check_multi_scope_tpm(
        mock_redis, descriptors, reserved_tokens=50000
    )
    assert result.allowed is False
    assert result.scope == "TEAM"
    assert result.limit_type == "tpm"


@pytest.mark.asyncio
async def test_settle_tpm_refund(mock_redis, monkeypatch):
    """예약 > 실제 사용 → 예약한 스코프마다 rate_limit_tpm_settle EVAL 한 번(차액, 예약 버킷,
    지금 버킷). 파이프라인 1회. 키를 직접 만들 수 있는 INCRBY 는 쓰지 않는다.
    """
    from app.services import rate_limit_service as rls
    from app.services.lua_loader import LuaScriptLoader
    from app.services.rate_limit_scope import build_tpm_key_group

    monkeypatch.setattr(rls, "time", SimpleNamespace(time=lambda: 29_000_000 * 60 + 5.0))
    LuaScriptLoader._scripts["rate_limit_tpm_settle"] = "-- mock"
    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=None,
        user_tpm=10000,
        team_rpm=None,
        team_tpm=100000,
        global_rpm=None,
        global_tpm=1000000,
    )

    keys = tuple(
        (*build_tpm_key_group(d.scope, d.scope_id, d.model_alias), 29_000_000)
        for d in descriptors
    )
    svc = RateLimitService()
    await svc.settle_tpm(
        mock_redis, descriptors, reserved_tokens=5000, actual_tokens=3500, reserved_keys=keys,
        reservation_id="rid-1",
    )
    pipe = mock_redis.pipeline.return_value
    assert pipe.incrby.call_count == 0
    assert [c.args[2:] for c in pipe.eval.call_args_list] == [
        (cur, prev, win, cur.replace(":tpm:cur", ":tpm-settled:rid-1"), "-1500", "29000000",
         "180", "120", "29000000")
        for cur, prev, win, _b in keys
    ]
    pipe.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_settle_tpm_without_reserved_keys_writes_nothing(mock_redis):
    """예약 키가 없는(옛 모양의) 정산 상태 → Redis 에 아무것도 보내지 않는다."""
    descriptors = build_scope_descriptors(
        user_id="u1", team_id="t1", model_alias="m1",
        user_rpm=None, user_tpm=10000, team_rpm=None, team_tpm=100000,
        global_rpm=None, global_tpm=1000000,
    )
    for keys in (None, ()):
        await RateLimitService().settle_tpm(
            mock_redis, descriptors, reserved_tokens=5000, actual_tokens=0, reserved_keys=keys
        )
    mock_redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_check_multi_scope_tpm_records_the_reserved_scopes_and_bucket(
    mock_redis, monkeypatch
):
    """통과한 스코프만 (cur, prev, window, 버킷)으로 기록한다. eval 이 실패해 fail-open 한
    스코프는 빠진다. 버킷 = 예약 시각 // 60(Lua 의 current_bucket 과 같은 값)."""
    from app.services import rate_limit_service as rls
    from app.services.lua_loader import LuaScriptLoader
    from app.services.rate_limit_scope import build_tpm_key_group

    LuaScriptLoader._scripts["rate_limit_tpm_check"] = "-- mock"
    monkeypatch.setattr(rls, "time", SimpleNamespace(time=lambda: 1_740_000_059.9))
    ok = json.dumps({"allowed": True, "limit": -1, "remaining": -1}).encode()
    mock_redis.eval = AsyncMock(side_effect=[ok, ConnectionError("shard down"), ok])
    descriptors = build_scope_descriptors(
        user_id="u1", team_id="t1", model_alias="m1",
        user_rpm=None, user_tpm=10000, team_rpm=None, team_tpm=100000,
        global_rpm=None, global_tpm=1000000,
    )
    result = await RateLimitService().check_multi_scope_tpm(
        mock_redis, descriptors, reserved_tokens=500
    )
    assert result.allowed
    bucket = 1_740_000_059 // 60
    assert result.reserved_keys == (
        (*build_tpm_key_group(RateLimitScope.USER, "u1", "m1"), bucket),
        (*build_tpm_key_group(RateLimitScope.GLOBAL, None, "m1"), bucket),
    )


@pytest.mark.asyncio
async def test_settle_tpm_noop_when_adjustment_zero(mock_redis):
    descriptors = build_scope_descriptors(
        user_id="u1",
        team_id="t1",
        model_alias="m1",
        user_rpm=None,
        user_tpm=10000,
        team_rpm=None,
        team_tpm=100000,
        global_rpm=None,
        global_tpm=1000000,
    )

    svc = RateLimitService()
    await svc.settle_tpm(
        mock_redis, descriptors, reserved_tokens=5000, actual_tokens=5000
    )
    # 예약한 키가 없고(옛 모양의 상태) adjustment==0 → 파이프라인 자체를 만들지 않는다.
    mock_redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_settle_tpm_with_zero_adjustment_still_sets_the_markers_in_one_pipeline(mock_redis):
    """실제 == 예약이어도 예약한 스코프마다 정산 EVAL 을 보낸다(차액 "0") — 정산 표지를 세워, 나중에
    같은 예약을 다시 정산하려는 호출을 막는다. 파이프라인은 한 번이다(흔한 경로와 같다)."""
    from app.services.lua_loader import LuaScriptLoader
    from app.services.rate_limit_scope import build_tpm_key_group

    LuaScriptLoader._scripts["rate_limit_tpm_settle"] = "-- mock"
    keys = ((*build_tpm_key_group(RateLimitScope.USER, "u1", "m1"), 29_000_000),)
    await RateLimitService().settle_tpm(
        mock_redis, [], reserved_tokens=5000, actual_tokens=5000, reserved_keys=keys,
        reservation_id="rid-0",
    )
    pipe = mock_redis.pipeline.return_value
    assert [c.args[4:7] for c in pipe.eval.call_args_list] == [
        (keys[0][2], keys[0][0].replace(":tpm:cur", ":tpm-settled:rid-0"), "0")
    ]
    pipe.execute.assert_awaited_once()


# ─── FR-4.6 CPM/CPH (USER + TEAM) ───


@pytest.mark.asyncio
async def test_reserve_cost_user_and_team_pass(mock_redis):
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["cost_rate_limit_scope"] = "-- mock"
    mock_redis.eval = AsyncMock(
        return_value=json.dumps({"allowed": True, "reserved_cost": 0.005}).encode()
    )

    svc = RateLimitService()
    result = await svc.reserve_cost(
        mock_redis,
        user_id="u1",
        estimated_cost=Decimal("0.005"),
        user_cpm_limit=Decimal("1.0"),
        user_cph_limit=Decimal("10.0"),
        team_id="t1",
        team_cpm_limit=Decimal("5.0"),
        team_cph_limit=Decimal("50.0"),
    )
    assert result.allowed is True
    assert result.reserved_cost == Decimal("0.005")
    # CROSSSLOT 대응(deepdive Q50): scope 별 분리 호출 → eval 2회(USER, TEAM),
    # 각 numkeys=3(cpm/cph 와 예약 표지는 동일 hash tag = 단일 슬롯). ARGV 는 비용, cpm·cph
    # 한도, scope_label, 표지 TTL 순서다.
    assert mock_redis.eval.await_count == 2
    labels = [call.args[-2] for call in mock_redis.eval.await_args_list]
    assert labels == ["USER", "TEAM"]
    for call, sid in zip(mock_redis.eval.await_args_list, ("user:{u1}", "team:{t1}")):
        assert call.args[1] == 3
        assert call.args[4].startswith(f"rl:cost-reserved:{sid}:")


@pytest.mark.asyncio
async def test_reserve_cost_user_cpm_exceeded_returns_user_scope(mock_redis):
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["cost_rate_limit_scope"] = "-- mock"
    mock_redis.eval = AsyncMock(
        return_value=json.dumps(
            {
                "allowed": False,
                "scope": "USER",
                "limit_type": "cpm",
                "limit": 1.0,
                "remaining": 0.2,
                "retry_after": 60,
                "reserved_cost": 0,
            }
        ).encode()
    )

    svc = RateLimitService()
    result = await svc.reserve_cost(
        mock_redis,
        user_id="u1",
        estimated_cost=Decimal("0.9"),
        user_cpm_limit=Decimal("1.0"),
        user_cph_limit=Decimal("10.0"),
    )
    assert result.allowed is False
    assert result.scope == "USER"
    assert result.limit_type == "cpm"
    assert result.retry_after == 60


@pytest.mark.asyncio
async def test_reserve_cost_team_cph_exceeded_returns_team_scope(mock_redis):
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["cost_rate_limit_scope"] = "-- mock"
    # scope 별 분리 호출: USER 통과 → TEAM 거부 (fast-fail). side_effect 로 순서 모사.
    mock_redis.eval = AsyncMock(
        side_effect=[
            json.dumps({"allowed": True, "reserved_cost": 5.0}).encode(),
            json.dumps(
                {
                    "allowed": False,
                    "scope": "TEAM",
                    "limit_type": "cph",
                    "limit": 50.0,
                    "remaining": 2.0,
                    "retry_after": 3600,
                    "reserved_cost": 0,
                }
            ).encode(),
        ]
    )

    svc = RateLimitService()
    result = await svc.reserve_cost(
        mock_redis,
        user_id="u1",
        estimated_cost=Decimal("5.0"),
        user_cpm_limit=Decimal("10.0"),
        user_cph_limit=Decimal("100.0"),
        team_id="t1",
        team_cpm_limit=Decimal("50.0"),
        team_cph_limit=Decimal("50.0"),
    )
    assert result.allowed is False
    assert result.scope == "TEAM"
    assert result.limit_type == "cph"
    assert mock_redis.eval.await_count == 2  # USER 통과 후 TEAM 검사


@pytest.mark.asyncio
async def test_reserve_cost_unlimited_skips_lua(mock_redis):
    """USER/TEAM 모두 한도 미설정이면 Lua 호출 없이 즉시 통과."""
    mock_redis.eval = AsyncMock()

    svc = RateLimitService()
    result = await svc.reserve_cost(
        mock_redis,
        user_id="u1",
        estimated_cost=Decimal("100"),
        user_cpm_limit=None,
        user_cph_limit=None,
    )
    assert result.allowed is True
    assert result.reserved_cost == Decimal("0")
    mock_redis.eval.assert_not_called()


_USER_KEYS = ("rl:cost:user:{u1}:cpm:120", "rl:cost:user:{u1}:cph:0")
_TEAM_KEYS = ("rl:cost:team:{t1}:cpm:120", "rl:cost:team:{t1}:cph:0")


@pytest.mark.asyncio
async def test_settle_cost_evals_each_reserved_scope_once(mock_redis, monkeypatch):
    """정산 = 예약한 스코프마다 cost_settle_scope EVAL 한 번(예약한 두 키, 표지, 지금 창의 두 키,
    차액). 파이프라인 1회. 지금이 예약한 창 안이면 지금 창의 키는 예약한 키와 같다."""
    from app.services import rate_limit_service as rls
    from app.services.lua_loader import LuaScriptLoader

    monkeypatch.setattr(rls, "time", SimpleNamespace(time=lambda: 150.0))  # cpm 120 · cph 0 창
    LuaScriptLoader._scripts["cost_settle_scope"] = "-- mock"
    svc = RateLimitService()
    await svc.settle_cost(
        mock_redis,
        user_id="u1",
        actual_cost=Decimal("0.003"),
        reserved_cost=Decimal("0.010"),
        team_id="t1",
        reserved_keys=(_USER_KEYS, _TEAM_KEYS),
        reservation_id="rid-1",
    )
    pipe = mock_redis.pipeline.return_value
    calls = [c.args for c in pipe.eval.call_args_list]
    assert calls == [
        ("-- mock", 5, *_USER_KEYS, "rl:cost-settled:user:{u1}:rid-1", *_USER_KEYS, "-0.007",
         "120"),
        ("-- mock", 5, *_TEAM_KEYS, "rl:cost-settled:team:{t1}:rid-1", *_TEAM_KEYS, "-0.007",
         "120"),
    ]
    pipe.incrbyfloat.assert_not_called()  # 키를 직접 만들 수 있는 INCRBYFLOAT 는 쓰지 않는다
    pipe.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_settle_cost_touches_only_the_reserved_scope(mock_redis):
    """USER 만 예약했으면 team_id 가 있어도 TEAM 키는 건드리지 않는다(옛 정산은 무조건 조정했다)."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["cost_settle_scope"] = "-- mock"
    await RateLimitService().settle_cost(
        mock_redis,
        user_id="u1",
        actual_cost=Decimal("0"),
        reserved_cost=Decimal("0.010"),
        team_id="t1",
        reserved_keys=(_USER_KEYS,),
    )
    keys = [k for c in mock_redis.pipeline.return_value.eval.call_args_list for k in c.args[2:4]]
    assert keys == list(_USER_KEYS)


@pytest.mark.asyncio
async def test_settle_cost_without_reserved_keys_writes_nothing(mock_redis):
    """예약 키가 없는(옛 모양의) 정산 상태 → Redis 에 아무것도 보내지 않는다."""
    for keys in (None, ()):
        await RateLimitService().settle_cost(
            mock_redis,
            user_id="u1",
            actual_cost=Decimal("0"),
            reserved_cost=Decimal("0.010"),
            team_id="t1",
            reserved_keys=keys,
        )
    mock_redis.pipeline.assert_not_called()
    mock_redis.eval.assert_not_called()


@pytest.mark.asyncio
async def test_reserve_cost_records_only_the_scopes_it_reserved(mock_redis):
    """reserved_keys = 실제로 예약한 스코프의 (cpm, cph) 키. 한도 없음·fail-open 은 빠진다."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["cost_rate_limit_scope"] = "-- mock"
    ok = json.dumps({"allowed": True, "reserved_cost": 0.5}).encode()
    svc = RateLimitService()

    # USER 한도만 — TEAM 은 team_id 가 있어도 예약하지 않는다.
    mock_redis.eval = AsyncMock(return_value=ok)
    r = await svc.reserve_cost(
        mock_redis, user_id="u1", estimated_cost=Decimal("0.5"),
        user_cpm_limit=Decimal("1"), user_cph_limit=None, team_id="t1",
    )
    assert r.allowed and r.reserved_cost == Decimal("0.5")
    assert [tuple(k.split(":")[2:4]) for pair in r.reserved_keys for k in pair] == [
        ("user", "{u1}"), ("user", "{u1}")
    ]
    cpm_key, cph_key = r.reserved_keys[0]
    assert cpm_key.split(":")[4] == "cpm" and cph_key.split(":")[4] == "cph"

    # TEAM eval 이 실패(fail-open) — 통과하지만 TEAM 키는 기록하지 않는다.
    mock_redis.eval = AsyncMock(side_effect=[ok, ConnectionError("shard down")])
    r = await svc.reserve_cost(
        mock_redis, user_id="u1", estimated_cost=Decimal("0.5"),
        user_cpm_limit=Decimal("1"), user_cph_limit=None,
        team_id="t1", team_cpm_limit=Decimal("5"), team_cph_limit=None,
    )
    assert r.allowed and len(r.reserved_keys) == 1 and "user" in r.reserved_keys[0][0]

    # 전부 fail-open — 예약한 키가 없으니 reserved_cost 도 0(정산이 돌지 않는다).
    mock_redis.eval = AsyncMock(side_effect=ConnectionError("down"))
    r = await svc.reserve_cost(
        mock_redis, user_id="u1", estimated_cost=Decimal("0.5"),
        user_cpm_limit=Decimal("1"), user_cph_limit=None,
    )
    assert r.allowed and r.reserved_keys == () and r.reserved_cost == Decimal("0")

    # 예약액 0 — Lua 가 쓰지 않으므로(`if cost > 0`) 기록할 키도 없다.
    zero = json.dumps({"allowed": True, "reserved_cost": 0}).encode()
    mock_redis.eval = AsyncMock(return_value=zero)
    r = await svc.reserve_cost(
        mock_redis, user_id="u1", estimated_cost=Decimal("0"),
        user_cpm_limit=Decimal("1"), user_cph_limit=None,
    )
    assert r.allowed and r.reserved_keys == () and r.reserved_cost == Decimal("0")


@pytest.mark.asyncio
async def test_settle_cost_noop_when_no_difference(mock_redis):
    svc = RateLimitService()
    await svc.settle_cost(
        mock_redis,
        user_id="u1",
        actual_cost=Decimal("0.005"),
        reserved_cost=Decimal("0.005"),
        team_id="t1",
    )
    # 예약한 키가 없고 차액 0 → 파이프라인 미생성.
    mock_redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_settle_cost_with_zero_difference_still_sets_the_markers_in_one_pipeline(mock_redis):
    """실제 == 예약이어도 예약한 스코프마다 정산 EVAL(차액 "0")을 보내 정산 표지를 세운다.
    파이프라인은 한 번이다."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["cost_settle_scope"] = "-- mock"
    await RateLimitService().settle_cost(
        mock_redis, user_id="u1", actual_cost=Decimal("0.005"), reserved_cost=Decimal("0.005"),
        team_id="t1", reserved_keys=(_USER_KEYS, _TEAM_KEYS), reservation_id="rid-0",
    )
    pipe = mock_redis.pipeline.return_value
    calls = [c.args for c in pipe.eval.call_args_list]
    assert [(c[4], c[7]) for c in calls] == [
        ("rl:cost-settled:user:{u1}:rid-0", "0.000"),
        ("rl:cost-settled:team:{t1}:rid-0", "0.000"),
    ]
    pipe.execute.assert_awaited_once()


# ─── CROSSSLOT 회귀 가드 (deepdive Q50) ───
#
# Redis Cluster 는 한 명령(여기선 EVAL)의 모든 키가 같은 hash slot 에 있어야 한다.
# 키의 {…} 안 문자열이 hash tag 이고, 같은 tag = 같은 slot. 과거 multi-scope EVAL
# 은 USER/TEAM/GLOBAL 키(tag 가 서로 다름)를 한 eval 에 묶어 cluster 에서 CROSSSLOT
# → fail-open 으로 enforcement 가 무음 정지됐다. 아래 가드는 **어떤 단일 eval 도
# 2개 이상의 서로 다른 hash tag 키를 받지 않음**을 잠가, 미래의 재-병합을 시끄럽게
# 깨뜨린다(단일노드 dev 에선 안 잡히던 버그를 단위테스트로 승격).


def _hash_tag(key: str) -> str:
    """Redis Cluster hash tag 추출 — 첫 '{' 와 그 뒤 첫 '}' 사이. 없으면 키 전체."""
    lo = key.find("{")
    if lo == -1:
        return key
    hi = key.find("}", lo + 1)
    if hi == -1 or hi == lo + 1:
        return key
    return key[lo + 1 : hi]


def _assert_eval_calls_single_slot(mock_redis) -> int:
    """기록된 모든 eval 호출이 단일 hash slot(키 1종 tag)만 쓰는지 검증.

    eval(script, numkeys, *keys, *argv) → keys = args[2 : 2+numkeys].
    """
    seen = 0
    for call in mock_redis.eval.await_args_list:
        numkeys = call.args[1]
        keys = call.args[2 : 2 + numkeys]
        tags = {_hash_tag(k) for k in keys}
        assert len(tags) <= 1, f"CROSSSLOT: 한 eval 에 복수 hash tag {tags} (키 {keys})"
        seen += 1
    return seen


@pytest.mark.asyncio
async def test_rpm_eval_never_crosses_slots(mock_redis):
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_check"] = "-- mock"
    descriptors = build_scope_descriptors(
        user_id="u1", team_id="t1", model_alias="m1",
        user_rpm=60, user_tpm=None, team_rpm=600, team_tpm=None,
        global_rpm=10000, global_tpm=None,
    )
    await RateLimitService().check_multi_scope_rpm(mock_redis, descriptors)
    assert _assert_eval_calls_single_slot(mock_redis) == 3


@pytest.mark.asyncio
async def test_tpm_eval_never_crosses_slots(mock_redis):
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["rate_limit_tpm_check"] = "-- mock"
    descriptors = build_scope_descriptors(
        user_id="u1", team_id="t1", model_alias="m1",
        user_rpm=None, user_tpm=10000, team_rpm=None, team_tpm=100000,
        global_rpm=None, global_tpm=1000000,
    )
    await RateLimitService().check_multi_scope_tpm(mock_redis, descriptors, reserved_tokens=5000)
    assert _assert_eval_calls_single_slot(mock_redis) == 3


@pytest.mark.asyncio
async def test_cost_eval_never_crosses_slots(mock_redis):
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["cost_rate_limit_scope"] = "-- mock"
    mock_redis.eval = AsyncMock(
        return_value=json.dumps({"allowed": True, "reserved_cost": 0.005}).encode()
    )
    await RateLimitService().reserve_cost(
        mock_redis,
        user_id="u1",
        estimated_cost=Decimal("0.005"),
        user_cpm_limit=Decimal("1.0"),
        user_cph_limit=Decimal("10.0"),
        team_id="t1",
        team_cpm_limit=Decimal("5.0"),
        team_cph_limit=Decimal("50.0"),
    )
    assert _assert_eval_calls_single_slot(mock_redis) == 2


@pytest.mark.asyncio
async def test_cost_settle_eval_never_crosses_slots(mock_redis):
    """정산 EVAL 도 스코프마다 한 슬롯 — 두 스코프의 키를 한 EVAL 에 섞지 않는다."""
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader._scripts["cost_settle_scope"] = "-- mock"
    await RateLimitService().settle_cost(
        mock_redis,
        user_id="u1",
        actual_cost=Decimal("0"),
        reserved_cost=Decimal("0.010"),
        team_id="t1",
        reserved_keys=(_USER_KEYS, _TEAM_KEYS),
    )
    evals = mock_redis.pipeline.return_value.eval.call_args_list
    assert len(evals) == 2
    for call in evals:
        numkeys = call.args[1]
        keys = call.args[2 : 2 + numkeys]
        assert len({_hash_tag(k) for k in keys}) == 1, f"CROSSSLOT: {keys}"


@pytest.mark.asyncio
async def test_a_later_scope_denial_releases_the_earlier_reservations(mock_redis):
    """호출 모양 가드(값의 증명은 test_medium_reservation_release_on_denial.py, 실 Redis): TEAM 이
    거부하면 USER 에 넣은 예약을 예약액의 음수로 한 번 되돌린다 — 비용은 cost_settle_scope, TPM 은
    rate_limit_tpm_settle(예약한 버킷)."""
    from app.services.lua_loader import LuaScriptLoader

    for name in ("cost_rate_limit_scope", "cost_settle_scope", "rate_limit_tpm_check",
                 "rate_limit_tpm_settle"):
        LuaScriptLoader._scripts[name] = f"-- {name}"
    ok = json.dumps({"allowed": True, "reserved_cost": 0.5}).encode()
    deny = json.dumps({"allowed": False, "scope": "TEAM", "limit_type": "cpm"}).encode()
    mock_redis.eval = AsyncMock(side_effect=[ok, deny])
    r = await RateLimitService().reserve_cost(
        mock_redis, user_id="u1", estimated_cost=Decimal("0.5"),
        user_cpm_limit=Decimal("1"), user_cph_limit=None,
        team_id="t1", team_cpm_limit=Decimal("1"), team_cph_limit=None,
    )
    assert not r.allowed and r.reserved_cost == Decimal("0")
    released = [c.args for c in mock_redis.pipeline.return_value.eval.call_args_list]
    assert len(released) == 1
    script, numkeys, cpm_key, cph_key, marker, _cpm_now, _cph_now, adj, _ttl = released[0]
    assert (script, numkeys, adj) == ("-- cost_settle_scope", 5, "-0.5")
    assert marker.startswith("rl:cost-settled:user:{u1}:")
    assert cpm_key.startswith("rl:cost:user:{u1}:cpm:")
    assert cph_key.startswith("rl:cost:user:{u1}:cph:")

    mock_redis.pipeline.return_value.eval.reset_mock()
    ok_tpm = json.dumps({"allowed": True, "limit": -1, "remaining": -1}).encode()
    deny_tpm = json.dumps({"allowed": False, "scope": "TEAM", "limit_type": "tpm"}).encode()
    mock_redis.eval = AsyncMock(side_effect=[ok_tpm, deny_tpm])
    descriptors = build_scope_descriptors(
        user_id="u1", team_id="t1", model_alias="m1",
        user_rpm=None, user_tpm=10000, team_rpm=None, team_tpm=100,
        global_rpm=None, global_tpm=None,
    )
    t = await RateLimitService().check_multi_scope_tpm(mock_redis, descriptors, reserved_tokens=500)
    assert not t.allowed and t.reserved_keys == ()
    released = [c.args for c in mock_redis.pipeline.return_value.eval.call_args_list]
    assert len(released) == 1
    assert released[0][0] == "-- rate_limit_tpm_settle" and released[0][1] == 4
    assert released[0][2] == "{USER:u1:m1}:tpm:cur" and released[0][6] == "-500"
