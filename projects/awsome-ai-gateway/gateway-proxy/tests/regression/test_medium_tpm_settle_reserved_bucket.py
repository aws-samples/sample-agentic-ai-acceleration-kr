# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""TPM 정산은 **예약한 스코프의, 예약한 버킷**만 조정하고 TTL 없는 키도 음수도 만들지 않는다.

무엇이 문제였나
---------------
TPM 은 스코프마다 세 키(``{SCOPE:id:model}:tpm:cur`` · ``:prev`` · ``:window``)로 1분 버킷을 굴린다.
``settle_tpm`` 은 TPM 한도가 있는 스코프마다 **지금의** cur 를 INCRBY 했다. 그래서

  (1) 예약과 정산 사이에 버킷이 회전했으면(다른 요청이 다음 분에 들어오면) **다음 분의** cur 에서
      환불해 음수로 만들었고, 예약한 분(이제 prev)은 예약액을 그대로 들고 있었다.
  (2) cur 키가 만료된 뒤(스트림이 TTL 180초를 넘긴 경우)에는 TTL 없는 음수 키를 새로 만들었다
      (실측: −3800, TTL −1).
  (3) eval 이 실패해 예약하지 않은(fail-open) 스코프도 조정했다.

검사 스크립트(``rate_limit_tpm_check.lua``)도 TTL 없는 키를 남겼다: 회전할 때 SET 이 TTL 을 지우는데
prev 에는 EXPIRE 를 한 번도 걸지 않았고, 회전 직후 거부되면 세 키가 모두 TTL 없이 남았다.

이 파일
-------
실 Redis 와 실 Lua. 예약은 ``enforce_rate_limits`` 그대로(한도 로더만 대역), 정산은 실제 호출
지점(``CostRecorder.finalize``, ``release_reservations``)으로만 한다. 시계는 rate_limit_service 의
``time`` 만 바꾼다(버킷 번호). 시나리오마다 ``{*}:tpm:*`` 전체를 SCAN 해 TTL −1 키가 0 개이고 음수가
없음을, 그리고 각 버킷의 값이 기대값과 정확히 같음을 단정한다. 모든 시나리오가 단일 노드
(``REDIS_PROOF_URL``)와 진짜 Redis Cluster(``REDIS_CLUSTER_URL``)에서 한 번씩 돈다 —
USER·TEAM·GLOBAL 은 hash tag 가 달라 클러스터에서 서로 다른 슬롯이다. env 가 없으면 그 변형은
skip 한다.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from structlog.testing import capture_logs

import app
from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    Role,
    TokenUsage,
)
from app.services.cost_recorder import CostRecorder
from app.services.fallback_loop import release_reservations
from app.services.lua_loader import LuaScriptLoader
from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits
from app.services.rate_limit_enforcement import enforce_rate_limits
from app.services.rate_limit_scope import RateLimitScope, build_tpm_key_group, compute_tpm_incr

#: 테스트하는 패키지 자신의 Lua — ``pytest -o pythonpath=<다른 트리>/src`` 로 다른 코드를 돌리면 그
#: 트리의 스크립트도 같이 쓴다(대조군이 검사 스크립트까지 그 트리 것이어야 한다).
_SCRIPTS = Path(app.__file__).resolve().parent / "redis_scripts"
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")
REDIS_CLUSTER_URL = os.environ.get("REDIS_CLUSTER_URL")

_USAGE = TokenUsage(input_tokens=200, output_tokens=1000, total_tokens=1200)
_ACTUAL = compute_tpm_incr(_USAGE)  # 1200

#: 실 Redis 클라이언트 설정. ⚠️ 재시도를 끈다 — redis-py 는 응답이 socket_timeout 뒤에 온 **직접**
#: EVAL(예약 검사)을 다시 보내 서버에서 두 번 실행한다(실측: INCRBY 100 한 번이 200 이 됐다).
#: 그러면 부하가 큰 머신에서 카운터가 조용히 틀려 로직 결함처럼 보인다(load average 150–300 에서
#: ``…leaves_the_actual_tokens[cluster-partial]`` 가 한 번 실패한 것). 늦은 응답은 오류로 드러나야
#: 한다. 30초는 그런 부하에서도 거의 넘지 않는다.
_CLIENT_KW = {"socket_timeout": 30, "retry": Retry(NoBackoff(), 0)}


def _client(kind: str):
    if kind == "single":
        if not REDIS_PROOF_URL:
            pytest.skip(
                "REDIS_PROOF_URL 미설정 — 실 Redis 필요. 정산은 INCRBY/EXPIRE 로 카운터와 TTL 을 "
                "움직이는 것이라, 호출 여부만 보는 가짜로는 TTL 없는 키도 음수도 보이지 않는다."
            )
        import redis.asyncio as aioredis

        return aioredis.from_url(REDIS_PROOF_URL, **_CLIENT_KW)
    if not REDIS_CLUSTER_URL:
        pytest.skip(
            "REDIS_CLUSTER_URL 미설정 — 진짜 Redis Cluster 필요(단일 노드는 CROSSSLOT 을 "
            "일으키지 않는다, 가짜/단일노드 폴백 금지)."
        )
    from redis.asyncio.cluster import RedisCluster

    return RedisCluster.from_url(REDIS_CLUSTER_URL, **_CLIENT_KW)


@pytest.fixture(params=["single", "cluster"])
async def real_redis(request):
    client = _client(request.param)
    LuaScriptLoader.load_all(_SCRIPTS)
    try:
        yield client
    finally:
        await client.aclose()


class _Clock:
    """rate_limit_service 의 ``time`` 대역 — 버킷 번호(키 이름이 아니라 window 마커 값)만 바꾼다."""

    def __init__(self, monkeypatch) -> None:
        from app.services import rate_limit_service as rls

        self.now = (1_800_000_000 // 60) * 60 + 10.0
        monkeypatch.setattr(rls, "time", SimpleNamespace(time=lambda: self.now))

    @property
    def bucket(self) -> int:
        return int(self.now) // 60


class _Case:
    """한 시나리오의 사용자·팀·모델(GLOBAL 키가 테스트끼리 겹치지 않게 모델 alias 도 새로)."""

    def __init__(self) -> None:
        run = uuid.uuid4().hex[:10]
        self.auth = AuthContext(
            user_id=f"ts-u-{run}",
            team_id=f"ts-t-{run}",
            dept_id=str(uuid.uuid4()),
            roles=[Role.USER],
            auth_type=AuthType.VIRTUAL_KEY,
        )
        self.model = ModelConfigSchema(
            provider_model_id="anthropic.claude-sonnet-4-6",
            alias=f"ts-m-{run}",
            provider=ProviderType.BEDROCK,
            api_format=ApiFormat.ANTHROPIC_MESSAGES,
            pricing=ModelPricingSchema(
                input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")
            ),
            status=ModelStatus.ACTIVE,
        )
        self.groups = {
            "USER": build_tpm_key_group(RateLimitScope.USER, self.auth.user_id, self.model.alias),
            "TEAM": build_tpm_key_group(RateLimitScope.TEAM, self.auth.team_id, self.model.alias),
            "GLOBAL": build_tpm_key_group(RateLimitScope.GLOBAL, None, self.model.alias),
        }


def _limits(*, user=1_000_000, team=1_000_000, global_=10_000_000):
    """TPM 한도만 건다(RPM·비용 없음) — 정산 대상이 TPM 키뿐이게."""

    async def _load(**_kwargs):
        return AllScopeLimits(
            user=ScopeLimits(tpm=user),
            team=ScopeLimits(tpm=team),
            global_=ScopeLimits(tpm=global_),
        )

    return _load


async def _reserve(monkeypatch, r, case: _Case, *, max_tokens=4000, limits=None):
    monkeypatch.setattr(
        "app.services.rate_limit_enforcement.load_all_scope_limits", limits or _limits()
    )
    state: dict = {}
    rejected = await enforce_rate_limits(
        redis=r,
        auth_context=case.auth,
        model_config=case.model,
        body={
            "model": case.model.alias,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": "count my tokens"}],
        },
        state=state,
        request_id=f"req-{uuid.uuid4().hex[:8]}",
    )
    return state, rejected


async def _finalize(r, case: _Case, state, usage: TokenUsage) -> None:
    await CostRecorder().finalize(
        r,
        case.auth,
        case.model,
        usage,
        f"req-{uuid.uuid4().hex[:8]}",
        False,
        5,
        rate_limit_state=state.get("rate_limit_state"),
    )


def _text(raw) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def _tpm_keyspace(r, case: _Case) -> dict[str, tuple[int, int]]:
    """이 시나리오(사용자·팀·모델)의 ``{*}:tpm:*`` — {키: (값, TTL)}. 클러스터면 모든 primary.

    SCAN 패턴을 시나리오의 id·모델 alias 로 좁힌다 — 같은 PROOF 실행의 다른 테스트(같은 Redis 를
    쓰는 다른 실행도)가 남긴 키가 "TTL 없는 키·음수 없음" 단정을 오염시키지 않게.
    """
    out: dict[str, tuple[int, int]] = {}
    for pattern in (
        f"{{USER:{case.auth.user_id}:*}}:tpm:*",
        f"{{TEAM:{case.auth.team_id}:*}}:tpm:*",
        f"{{GLOBAL:*:{case.model.alias}}}:tpm:*",
    ):
        async for raw_key in r.scan_iter(match=pattern, count=500):
            key = _text(raw_key)
            raw = await r.get(key)
            if raw is None:
                continue
            out[key] = (int(float(_text(raw))), int(await r.ttl(key)))
    return out


def _assert_no_ttl_less_or_negative(keyspace: dict) -> None:
    ttl_less = {k: v for k, v in keyspace.items() if v[1] == -1}
    negative = {k: v for k, v in keyspace.items() if v[0] < 0}
    assert not ttl_less, f"TTL 없는 TPM 키 — 영원히 남고 volatile-lru 도 못 지운다: {ttl_less}"
    assert not negative, f"음수 TPM 카운터 — 그 분 동안 남의 한도를 빌려 준다: {negative}"


def _values(keyspace: dict, case: _Case) -> dict[str, dict[str, int]]:
    """{스코프: {"cur"/"prev"/"window": 값}} — 키가 없으면 그 칸이 없다."""
    out: dict[str, dict[str, int]] = {}
    for scope, keys in case.groups.items():
        out[scope] = {
            k.rsplit(":", 1)[-1]: keyspace[k][0] for k in keys if k in keyspace
        }
    return out


def _reserved(state) -> int:
    return int(state["rate_limit_state"]["tpm_reserved"])


async def _cleanup(r, case: _Case) -> None:
    for keys in case.groups.values():
        for key in keys:
            await r.delete(key)
    for pattern in (
        f"budget:user:{{{case.auth.user_id}}}:*",
        f"budget:team:{{{case.auth.team_id}}}:*",
    ):
        async for raw_key in r.scan_iter(match=pattern, count=500):
            await r.delete(raw_key)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 같은 버킷 — 실제 토큰만 남는다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("usage", [_USAGE, TokenUsage()], ids=["partial", "zero-usage"])
async def test_settle_in_the_reserved_bucket_leaves_the_actual_tokens(
    real_redis, monkeypatch, usage
):
    """USER·TEAM·GLOBAL 을 예약하고 같은 분에 정산 → 세 스코프의 cur = 실제 토큰(환불이면 0)."""
    r, case, clock = real_redis, _Case(), _Clock(monkeypatch)
    try:
        state, rejected = await _reserve(monkeypatch, r, case)
        assert rejected is None
        reserved = _reserved(state)
        assert reserved > _ACTUAL
        before = _values(await _tpm_keyspace(r, case), case)
        assert all(v["cur"] == reserved for v in before.values()), before

        await _finalize(r, case, state, usage)

        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        expect = compute_tpm_incr(usage)
        for scope, v in _values(keyspace, case).items():
            assert v == {"cur": expect, "prev": 0, "window": clock.bucket}, (scope, v)
    finally:
        await _cleanup(r, case)


async def test_extra_tokens_land_in_the_reserved_bucket(real_redis, monkeypatch):
    """실제 > 예약(max_tokens 가 작고 출력이 큼) → cur = 실제 토큰."""
    r, case, clock = real_redis, _Case(), _Clock(monkeypatch)
    big = TokenUsage(input_tokens=50, output_tokens=6000, total_tokens=6050)
    try:
        state, _ = await _reserve(monkeypatch, r, case, max_tokens=10)
        assert _reserved(state) < compute_tpm_incr(big)
        await _finalize(r, case, state, big)
        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        for v in _values(keyspace, case).values():
            assert v == {"cur": compute_tpm_incr(big), "prev": 0, "window": clock.bucket}
    finally:
        await _cleanup(r, case)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 버킷 회전 · 만료 — 다른 분의 카운터를 움직이지 않는다
# ─────────────────────────────────────────────────────────────────────────────


async def test_settle_after_one_rotation_adjusts_prev_not_the_new_bucket(real_redis, monkeypatch):
    """A 가 분 B 에 예약, 다른 요청 X 가 분 B+1 에 예약(회전: B → prev), 그 뒤 A 정산.

    A 의 차액은 prev(분 B)에 간다 — prev = A 의 실제 토큰, cur = X 의 예약 그대로. 옛 정산은 지금의
    cur(분 B+1)에서 A 의 환불을 빼 X 의 예약을 깎거나 음수로 만들고, prev 는 A 의 예약액을 그대로
    들었다.
    """
    r, case, clock = real_redis, _Case(), _Clock(monkeypatch)
    try:
        clock.now += 40  # 분 B 의 50초
        state_a, _ = await _reserve(monkeypatch, r, case)
        reserved_a = _reserved(state_a)
        b = clock.bucket
        clock.now += 15  # 분 B+1 의 5초
        state_x, _ = await _reserve(monkeypatch, r, case, max_tokens=500)
        reserved_x = _reserved(state_x)
        assert reserved_x < reserved_a - _ACTUAL  # 옛 정산이면 X 의 cur 가 음수가 된다

        await _finalize(r, case, state_a, _USAGE)

        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        for scope, v in _values(keyspace, case).items():
            assert v == {"cur": reserved_x, "prev": _ACTUAL, "window": b + 1}, (scope, v)
    finally:
        await _cleanup(r, case)


async def test_settle_after_two_rotations_writes_nothing(real_redis, monkeypatch):
    """A 의 분이 두 번 넘어간 뒤(그 분은 어떤 추정에도 들어가지 않는다) → 키가 바뀌지 않는다."""
    r, case, clock = real_redis, _Case(), _Clock(monkeypatch)
    try:
        state_a, _ = await _reserve(monkeypatch, r, case)
        b = clock.bucket
        clock.now += 120  # 분 B+2
        state_x, _ = await _reserve(monkeypatch, r, case, max_tokens=500)
        reserved_x = _reserved(state_x)
        before = _values(await _tpm_keyspace(r, case), case)

        await _finalize(r, case, state_a, _USAGE)

        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        after = _values(keyspace, case)
        assert after == before
        assert all(v == {"cur": reserved_x, "prev": 0, "window": b + 2} for v in after.values())
    finally:
        await _cleanup(r, case)


async def test_settle_after_the_bucket_keys_expired_creates_nothing(real_redis, monkeypatch):
    """세 키가 TTL 로 만료된 뒤 정산(스트림이 180초를 넘긴 경우) → 키를 만들지 않는다.

    옛 정산은 cur 를 INCRBY 해 TTL 없는 음수 키를 만들었다(실측 −3800, TTL −1).
    """
    import asyncio

    r, case, _clock = real_redis, _Case(), _Clock(monkeypatch)
    try:
        state, _ = await _reserve(monkeypatch, r, case)
        for keys in case.groups.values():
            for key in keys:
                await r.pexpire(key, 1)
        await asyncio.sleep(0.05)
        assert _values(await _tpm_keyspace(r, case), case) == {s: {} for s in case.groups}

        await _finalize(r, case, state, _USAGE)

        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        assert _values(keyspace, case) == {s: {} for s in case.groups}, "만료된 버킷이 되살아났다"
    finally:
        await _cleanup(r, case)


_BIG = TokenUsage(input_tokens=50, output_tokens=6000, total_tokens=6050)


@pytest.mark.parametrize("after", ["rotated", "not-rotated", "two-rotations", "expired"])
async def test_extra_tokens_after_the_reserved_bucket_land_in_the_current_bucket(
    real_redis, monkeypatch, after
):
    """실제 > 예약인데 정산 때 예약한 버킷 B 가 이미 지났다 → 추가분(실제 − 예약)은 **지금** 버킷의
    cur 에 간다. 예약분은 B 에 남아 회전 규칙대로 prev 로 가거나 사라진다. 세 스코프 모두.

    * rotated: 다른 요청 X 가 B+1 에 예약해 회전시켰다 → cur = X 의 예약 + 추가분, prev = A 의 예약.
    * not-rotated: B 뒤로 검사가 없었다 → 정산이 check 와 같은 회전을 한다 → prev = A 의 예약,
      cur = 추가분, window = B+1.
    * two-rotations: X 가 B+2 에 예약 → cur = X 의 예약 + 추가분, prev = 0.
    * expired: 세 키가 만료됐고 지금은 B+3 → cur = 추가분(새로 만든다), prev = 0, window = B+3.

    세 키 모두 TTL 이 있다. 예전에는 rotated 의 추가분이 prev 로 가 가중치만큼 줄었고, 나머지
    셋에서는 아무 데도 남지 않았다. 환불은 그대로 예약한 버킷에서만 한다(위 회전 테스트).
    """
    import asyncio

    r, case, clock = real_redis, _Case(), _Clock(monkeypatch)
    try:
        state_a, rejected = await _reserve(monkeypatch, r, case, max_tokens=10)
        assert rejected is None
        reserved_a = _reserved(state_a)
        extra = compute_tpm_incr(_BIG) - reserved_a
        assert extra > 0
        b = clock.bucket
        if after in ("rotated", "two-rotations"):
            clock.now += 60 if after == "rotated" else 120
            state_x, _ = await _reserve(monkeypatch, r, case, max_tokens=500)
            reserved_x = _reserved(state_x)
            if after == "rotated":
                expect = {"cur": reserved_x + extra, "prev": reserved_a, "window": b + 1}
            else:
                expect = {"cur": reserved_x + extra, "prev": 0, "window": b + 2}
        elif after == "not-rotated":
            clock.now += 60
            expect = {"cur": extra, "prev": reserved_a, "window": b + 1}
        else:
            for keys in case.groups.values():
                for key in keys:
                    await r.pexpire(key, 1)
            await asyncio.sleep(0.05)
            clock.now += 180
            expect = {"cur": extra, "prev": 0, "window": b + 3}

        await _finalize(r, case, state_a, _BIG)

        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        for scope, v in _values(keyspace, case).items():
            assert v == expect, (scope, v)
        ttls = {k: keyspace[k][1] for keys in case.groups.values() for k in keys}
        assert all(0 < t <= 180 for t in ttls.values()), ttls
    finally:
        await _cleanup(r, case)


async def test_a_denial_right_after_a_rotation_leaves_no_key_without_a_ttl(
    real_redis, monkeypatch
):
    """검사 스크립트: 회전한 직후 거부돼도 cur·prev·window 모두 TTL 이 있다(예전엔 셋 다 −1).

    처음 예약에서 만들어지는 prev 에도 TTL 이 있다(예전엔 처음부터 −1 이었다).
    """
    r, case, clock = real_redis, _Case(), _Clock(monkeypatch)
    try:
        state, rejected = await _reserve(monkeypatch, r, case)
        assert rejected is None
        first = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(first)
        assert all("prev" in v for v in _values(first, case).values())

        clock.now += 180  # 분 B+3 — 회전
        _state, rejected = await _reserve(
            monkeypatch, r, case, limits=_limits(user=10), max_tokens=4000
        )
        assert rejected is not None and rejected.status_code == 429

        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        user = _values(keyspace, case)["USER"]
        assert user == {"cur": 0, "prev": 0, "window": clock.bucket}
    finally:
        await _cleanup(r, case)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 되돌리기 · 옛 모양의 상태 · 예약하지 않은 스코프
# ─────────────────────────────────────────────────────────────────────────────


async def test_fallback_release_refunds_exactly_once(real_redis, monkeypatch):
    """``release_reservations`` → cur = 0. 미리 잡아 둔 사본으로 finalize 가 다시 돌아도 음수가
    되지 않는다."""
    r, case, _clock = real_redis, _Case(), _Clock(monkeypatch)
    try:
        state, _ = await _reserve(monkeypatch, r, case)
        captured = state["rate_limit_state"]
        await release_reservations(redis=r, state=state, auth_context=case.auth)
        await CostRecorder().finalize(
            r, case.auth, case.model, TokenUsage(), "req-c", True, 1, rate_limit_state=captured
        )
        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        assert all(v["cur"] == 0 for v in _values(keyspace, case).values())
    finally:
        await _cleanup(r, case)


async def test_legacy_state_without_tpm_keys_writes_nothing(real_redis, monkeypatch):
    """``tpm_reserved_keys`` 가 없는(이 필드 이전 모양의) 정산 상태 → 키를 쓰지 않고 경고한다."""
    r, case, _clock = real_redis, _Case(), _Clock(monkeypatch)
    try:
        state, _ = await _reserve(monkeypatch, r, case)
        reserved = _reserved(state)
        state["rate_limit_state"].pop("tpm_reserved_keys", None)
        with capture_logs() as logs:
            await _finalize(r, case, state, _USAGE)
        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        assert all(v["cur"] == reserved for v in _values(keyspace, case).values())
        assert [e["event"] for e in logs if e.get("event", "").startswith("tpm_settle")] == [
            "tpm_settle_skipped_no_reserved_keys"
        ]
    finally:
        await _cleanup(r, case)


async def test_a_scope_whose_check_failed_open_is_not_settled(real_redis, monkeypatch):
    """TEAM 의 검사 eval 이 실패(fail-open)하면 TEAM 은 예약되지 않았다 — 정산도 TEAM 키를 만들지
    않는다.

    옛 정산은 한도가 있는 스코프마다 cur 를 INCRBY 해 TEAM 에 TTL 없는 음수 키를 만들었다.
    """
    from app.services import rate_limit_service as rls

    r, case, _clock = real_redis, _Case(), _Clock(monkeypatch)
    team_cur = case.groups["TEAM"][0]
    real_eval = rls._guarded_eval

    async def _team_shard_down(redis, *args):
        if len(args) > 2 and args[2] == team_cur:
            raise ConnectionError("TEAM shard down")
        return await real_eval(redis, *args)

    monkeypatch.setattr(rls, "_guarded_eval", _team_shard_down)
    try:
        state, rejected = await _reserve(monkeypatch, r, case)
        assert rejected is None
        await _finalize(r, case, state, _USAGE)
        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        values = _values(keyspace, case)
        assert values["TEAM"] == {}, f"예약하지 않은 TEAM 에 키가 생겼다: {values['TEAM']}"
        assert values["USER"]["cur"] == _ACTUAL and values["GLOBAL"]["cur"] == _ACTUAL
    finally:
        await _cleanup(r, case)


async def test_a_negative_counter_left_by_the_old_settlement_is_clamped_and_gets_a_ttl(
    real_redis, monkeypatch
):
    """이 수정 이전 정산이 지금 분에 남긴 음수·TTL 없는 cur(−5000) 위에서 예약하고 환불하면,
    결과가 음수가 되는 대신 0 이 되고 세 키 모두 TTL 을 갖는다. 남은 음수는 그 분 동안 남의 한도를
    빌려 준다."""
    from app.services import rate_limit_service as rls

    r, case, clock = real_redis, _Case(), _Clock(monkeypatch)
    clamped = []

    class _Spy:
        def add(self, n, attrs=None):
            clamped.extend([(attrs or {}).get("kind")] * n)

    rls.set_settle_metrics(clamped=_Spy(), duplicate=None)
    try:
        for cur, _prev, win in case.groups.values():
            await r.set(cur, -5000)
            await r.set(win, clock.bucket)
        state, rejected = await _reserve(monkeypatch, r, case)
        assert rejected is None
        with capture_logs() as logs:
            await _finalize(r, case, state, _USAGE)
        keyspace = await _tpm_keyspace(r, case)
        _assert_no_ttl_less_or_negative(keyspace)
        assert all(v["cur"] == 0 for v in _values(keyspace, case).values())
        # 0 하한은 조용하지 않다 — 스코프마다 경고와 카운터.
        assert [e["event"] for e in logs if e.get("event") == "tpm_settle_clamped"] == [
            "tpm_settle_clamped"
        ] * 3
        assert clamped == ["tpm"] * 3
    finally:
        rls.set_settle_metrics(clamped=None, duplicate=None)
        await _cleanup(r, case)


@pytest.mark.parametrize("first", ["partial", "exact"], ids=["partial-refund", "zero-adjustment"])
async def test_a_second_settle_of_the_same_reservation_is_a_reported_no_op(
    real_redis, monkeypatch, first
):
    """같은 예약(같은 reservation_id)을 두 번 정산한다 — 결함이 보낼 두 번째 정산이다. 같은 버킷에
    다른 요청(bystander)의 예약이 함께 있다.

    두 번째 정산은 정산 표지에 막혀 아무것도 쓰지 않는다: 세 스코프의 cur = 다른 요청의 예약 + 이
    요청의 실제 토큰, 정확히. ``tpm_settle_duplicate`` 경고와 ``gateway_rl_settle_duplicate_total
    {kind=tpm}`` 가 스코프마다 한 번. ``zero-adjustment``: 첫 정산의 차액이 0 이어도 스크립트가 돌아
    표지를 세우므로 뒤따른 전액 환불(결함)도 막힌다.
    """
    from app.services import rate_limit_service as rls
    from app.services.rate_limit_service import RateLimitService

    r, case, _clock = real_redis, _Case(), _Clock(monkeypatch)
    clamped, duplicate = [], []

    class _Spy:
        def __init__(self, sink):
            self.sink = sink

        def add(self, n, attrs=None):
            self.sink.extend([(attrs or {}).get("kind")] * n)

    rls.set_settle_metrics(clamped=_Spy(clamped), duplicate=_Spy(duplicate))
    try:
        state_a, rejected = await _reserve(monkeypatch, r, case)
        assert rejected is None
        state_b, _ = await _reserve(monkeypatch, r, case)  # bystander, 같은 분
        a, b = state_a["rate_limit_state"], state_b["rate_limit_state"]
        reserved_a = int(a["tpm_reserved"])
        actual_a = _ACTUAL if first == "partial" else reserved_a
        settles = [actual_a, actual_a] if first == "partial" else [actual_a, 0]
        with capture_logs() as logs:
            for actual in settles:
                await RateLimitService().settle_tpm(
                    r, a["tpm_descriptors"], reserved_a, actual,
                    reserved_keys=a["tpm_reserved_keys"], reservation_id=a["reservation_id"],
                )
        values = _values(await _tpm_keyspace(r, case), case)
        expect = int(b["tpm_reserved"]) + actual_a
        assert all(v["cur"] == expect for v in values.values()), (expect, values)
        events = [e["event"] for e in logs if e["event"].startswith("tpm_settle_")]
        assert events == ["tpm_settle_duplicate"] * 3, events
        assert duplicate == ["tpm"] * 3 and clamped == []
    finally:
        rls.set_settle_metrics(clamped=None, duplicate=None)
        await _cleanup(r, case)
