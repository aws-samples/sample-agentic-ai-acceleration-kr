# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""비용(CPM/CPH) 정산은 **예약한 스코프의, 예약한 창의 키**만 조정하고 TTL 없는 키를 만들지 않는다.

무엇이 문제였나
---------------
``reserve_cost`` 는 한도가 있는 스코프(USER 및/또는 TEAM)만 예약하고, EXPIRE 는 예약 Lua
(``cost_rate_limit_scope.lua``) 안에서만 건다(cpm 120s / cph 7200s). 그런데 ``settle_cost`` 는

  * USER 키를 **무조건**, TEAM 키를 ``team_id`` 만 있으면 INCRBYFLOAT 했고,
  * 창을 **정산 시각의** ``now`` 로 골랐으며,
  * EXPIRE 를 걸지 않았다.

그래서 (1) 예약하지 않은 스코프에 음수 카운터가 TTL −1 로 생기고(이 파일을 수정 전 코드로 돌린 실측:
user 한도만 있을 때 zero-usage 환불 뒤 팀 cpm·cph 키 = −0.015078, TTL −1), (2) 분·시 경계를 넘긴
정산은 다음 창에 음수·TTL −1 키를 새로 만들며 예약한 창의 예약은 남고, (3) 예약 키가 만료된 뒤의
정산은 그 키를 TTL 없이 되살렸다. TTL 없는 키는 영원히 남고 ``volatile-lru`` 는 그것을 축출하지
못한다 — 메모리가 차면 TTL 이 있는 키(rate-limit 카운터·모델 캐시·예산 설정)가 먼저 밀려난다.
나중에 그 스코프에 한도를 켜면 쌓인 음수만큼 한도가 느슨해진다.

이 파일
-------
실 Redis 와 실 Lua 로 예약·정산한다. 예약은 ``enforce_rate_limits`` 그대로(한도 로더만 대역),
정산은 실제 호출 지점 셋 — ``CostRecorder.finalize`` 의 정상 정산과 zero-usage 환불,
``release_reservations`` — 으로만 한다. 시나리오마다 ``rl:cost:*`` 전체를 SCAN 해 TTL −1 키가
0 개이고 음수 카운터가 없음을, 그리고 이 요청의 키가 정확히 예약한 키뿐이며 값이 실제 비용
(환불이면 0)과 같음을 단정한다.

모든 시나리오는 두 번 돈다:

  * ``single`` — ``REDIS_PROOF_URL``(단일 노드). CI 의 PROOF 잡이 이것을 돌린다.
  * ``cluster`` — ``REDIS_CLUSTER_URL``(진짜 Redis Cluster, 3 primary + 3 replica). USER 와 TEAM
    키는 hash tag 가 달라 슬롯(대개 노드)이 다르다 — 두 스코프의 키를 한 EVAL 에 넣으면 클러스터에서
    CROSSSLOT 이고, 정산의 ``except`` 가 그것을 경고 한 줄로 삼켜 카운터가 조용히 그대로 남는다.
    단일 노드는 그 실패를 원리적으로 재현하지 못한다(``test_cluster_rejects_a_two_scope_settle``).

env 가 없으면 그 변형은 skip 한다 — 가짜 Redis 로 대신 통과시키지 않는다.

실행:
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 REDIS_CLUSTER_URL=redis://127.0.0.1:7001 \\
      pytest tests/regression/test_medium_cost_settle_reserved_scope.py

로컬 클러스터 하나(예): redis:7-alpine 컨테이너 하나에 클러스터 노드 6개(포트 7001-7006,
``--cluster-announce-ip 127.0.0.1``, 포트 7001-7006·17001-17006 publish) 를 띄우고
``redis-cli --cluster create 127.0.0.1:7001 … 127.0.0.1:7006 --cluster-replicas 1``.
"""

from __future__ import annotations

import asyncio
import os
import time
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
from app.services.cost_recorder import CostRecorder, calculate_cost
from app.services.fallback_loop import release_reservations
from app.services.lua_loader import LuaScriptLoader
from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits
from app.services.rate_limit_enforcement import enforce_rate_limits

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
#: 테스트하는 패키지 자신의 Lua — ``pytest -o pythonpath=<다른 트리>/src`` 로 다른 코드를 돌리면 그
#: 트리의 스크립트도 같이 쓴다(대조군이 정산 스크립트까지 그 트리 것이어야 한다).
_SCRIPTS = Path(app.__file__).resolve().parent / "redis_scripts"
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")
REDIS_CLUSTER_URL = os.environ.get("REDIS_CLUSTER_URL")

_MAX_TOKENS = 1000
_SMALL_USAGE = TokenUsage(input_tokens=10, output_tokens=100, total_tokens=110)

#: 실 Redis 클라이언트 설정. ⚠️ 재시도를 끈다 — redis-py 는 응답이 socket_timeout 뒤에 온 **직접**
#: EVAL(예약 검사)을 다시 보내 서버에서 두 번 실행한다(실측: INCRBY 100 한 번이 200 이 됐다).
#: 그러면 부하가 큰 머신에서 카운터가 조용히 틀려 로직 결함처럼 보인다(load average 150–300 에서
#: ``…leaves_the_actual_tokens[cluster-partial]`` 가 한 번 실패한 것). 늦은 응답은 오류로 드러나야
#: 한다. 30초는 그런 부하에서도 거의 넘지 않는다.
_CLIENT_KW = {"socket_timeout": 30, "retry": Retry(NoBackoff(), 0)}


def _client(kind: str):
    """실 Redis 클라이언트 — 운영과 같이 ``decode_responses`` 없이(bytes). env 가 없으면 skip."""
    if kind == "single":
        if not REDIS_PROOF_URL:
            pytest.skip(
                "REDIS_PROOF_URL 미설정 — 실 Redis 필요. 정산은 INCRBYFLOAT/EXPIRE 로 카운터와 "
                "TTL 을 움직이는 것이라, 호출 여부만 보는 가짜로는 TTL 없는 키도 음수도 "
                "보이지 않는다."
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
    """실 Redis — 단일 노드(REDIS_PROOF_URL)와 진짜 클러스터(REDIS_CLUSTER_URL)에서 한 번씩."""
    client = _client(request.param)
    LuaScriptLoader.load_all(_SCRIPTS)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def cluster_redis():
    client = _client("cluster")
    LuaScriptLoader.load_all(_SCRIPTS)
    try:
        yield client
    finally:
        await client.aclose()


# ── 도우미 ────────────────────────────────────────────────────────────────────────


def _auth() -> AuthContext:
    return AuthContext(
        user_id=f"cs-u-{uuid.uuid4().hex[:12]}",
        team_id=f"cs-t-{uuid.uuid4().hex[:12]}",
        dept_id=str(uuid.uuid4()),
        roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
    )


def _model() -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id="anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.ANTHROPIC_MESSAGES,
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")
        ),
        status=ModelStatus.ACTIVE,
    )


def _limits(*, user: bool, team: bool):
    """비용 한도만 건다(RPM/TPM 없음) — 정산 대상이 비용 키뿐이게."""

    def _scope(on: bool) -> ScopeLimits:
        return ScopeLimits(cpm=Decimal("100"), cph=Decimal("1000")) if on else ScopeLimits()

    async def _load(**_kwargs):
        return AllScopeLimits(user=_scope(user), team=_scope(team), global_=ScopeLimits())

    return _load


async def _reserve(monkeypatch, r, auth, *, user: bool, team: bool, max_tokens=_MAX_TOKENS):
    """라우터와 같은 진입점으로 예약한다.

    반환 = (라우터가 finalize/release 에 넘기는 state, 예약 직후 이 요청의 rl:cost 키 집합). 예약한
    키는 state 의 필드가 아니라 실제 keyspace 에서 읽는다 — 정산 전후를 같은 눈금으로 비교한다.
    """
    monkeypatch.setattr(
        "app.services.rate_limit_enforcement.load_all_scope_limits",
        _limits(user=user, team=team),
    )
    state: dict = {}
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "settle me"}],
    }
    rejected = await enforce_rate_limits(
        redis=r,
        auth_context=auth,
        model_config=_model(),
        body=body,
        state=state,
        request_id=f"req-{uuid.uuid4().hex[:8]}",
    )
    assert rejected is None, "예약 단계에서 거부됐다 — 한도 설정을 확인"
    return state, set(_of(await _cost_keyspace(r, auth), auth))


def _text(raw) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def _cost_keyspace(r, *auths: AuthContext) -> dict[str, tuple[float, int]]:
    """이 시나리오 사용자·팀의 ``rl:cost:*`` — {키: (값, TTL)}. 클러스터면 모든 primary 를 훑는다.

    SCAN 패턴을 시나리오의 id 로 좁힌다 — 같은 PROOF 실행의 다른 테스트(같은 Redis 를 쓰는 다른
    실행도)가 남긴 키가 "TTL 없는 키·음수 없음" 단정을 오염시키지 않게. 정산이 다른 창·다른 스코프에
    잘못 쓴 키도 같은 id 라 여전히 잡힌다.
    """
    out: dict[str, tuple[float, int]] = {}
    for auth in auths:
        for pattern in (
            f"rl:cost:user:{{{auth.user_id}}}:*",
            f"rl:cost:team:{{{auth.team_id}}}:*",
        ):
            async for raw_key in r.scan_iter(match=pattern, count=500):
                key = _text(raw_key)
                raw = await r.get(key)
                if raw is None:  # SCAN 과 GET 사이에 만료
                    continue
                out[key] = (float(_text(raw)), int(await r.ttl(key)))
    return out


def _of(keyspace: dict, auth: AuthContext) -> dict[str, tuple[float, int]]:
    tags = (f"{{{auth.user_id}}}", f"{{{auth.team_id}}}")
    return {k: v for k, v in keyspace.items() if any(t in k for t in tags)}


def _assert_no_ttl_less_or_negative(keyspace: dict) -> None:
    """⚠️ 이 파일의 핵심 단정 — ``rl:cost:*`` 어디에도 TTL 없는 키와 음수 카운터가 없다."""
    ttl_less = {k: v for k, v in keyspace.items() if v[1] == -1}
    negative = {k: v for k, v in keyspace.items() if v[0] < 0}
    assert not ttl_less, f"TTL 없는 rl:cost 키 — 영원히 남고 volatile-lru 도 못 지운다: {ttl_less}"
    assert not negative, f"음수 비용 카운터 — 그 창 동안 남의 한도를 빌려 준다: {negative}"


def _reserved(state: dict) -> float:
    return float(state["rate_limit_state"]["cost_reserved"])


async def _finalize(r, auth, state, usage: TokenUsage) -> None:
    await CostRecorder().finalize(
        r,
        auth,
        _model(),
        usage,
        f"req-{uuid.uuid4().hex[:8]}",
        False,
        5,
        rate_limit_state=state.get("rate_limit_state"),
    )


async def _cleanup(r, auth: AuthContext) -> None:
    for key in _of(await _cost_keyspace(r, auth), auth):
        await r.delete(key)
    for key in (
        f"budget:user:{{{auth.user_id}}}:*",
        f"budget:team:{{{auth.team_id}}}:*",
    ):
        async for raw_key in r.scan_iter(match=key, count=500):
            await r.delete(raw_key)


_SETTLE_EVENTS = ("cost_settle_failed", "cost_settle_skipped_no_reserved_keys")


def _settle_failures(logs: list[dict]) -> list[dict]:
    return [e for e in logs if e.get("event") in _SETTLE_EVENTS]


async def _clear_of_window_edge() -> None:
    """이 파일의 단정은 정산 뒤 SCAN 결과이지 시각으로 계산한 키 이름이 아니다. 그래도 예약 직후
    분이 바뀌면 경계 시나리오가 아닌 테스트까지 경계를 넘는다 — 분 끝 2초는 피한다."""
    while time.time() % 60 > 58:
        await asyncio.sleep(0.2)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 한도 구성별 — 예약한 스코프만 정산한다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "user,team",
    [(True, False), (False, True), (True, True)],
    ids=["user-only", "team-only", "both"],
)
async def test_settle_adjusts_only_the_scopes_that_were_reserved(
    real_redis, monkeypatch, user, team
):
    """한도가 있는 스코프만 예약되고, 정산 뒤 그 키들 = 실제 비용, 다른 스코프 키는 없다."""
    r = real_redis
    auth = _auth()
    await _clear_of_window_edge()
    try:
        state, keys = await _reserve(monkeypatch, r, auth, user=user, team=team)
        assert len(keys) == 2 * (int(user) + int(team))
        before = _of(await _cost_keyspace(r, auth), auth)
        assert all(v[0] == pytest.approx(_reserved(state)) and v[1] > 0 for v in before.values())

        actual = float(calculate_cost(_SMALL_USAGE, _model()))
        assert actual < _reserved(state)
        with capture_logs() as logs:
            await _finalize(r, auth, state, _SMALL_USAGE)

        keyspace = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(keyspace)
        after = _of(keyspace, auth)
        assert set(after) == keys, (
            f"예약하지 않은 스코프/창에 키가 생겼다: {sorted(set(after) - keys)}"
        )
        for key, (value, ttl) in after.items():
            assert value == pytest.approx(actual, abs=1e-12), f"{key} = {value}, 실제 {actual}"
            assert ttl > 0
        assert not _settle_failures(logs), _settle_failures(logs)
    finally:
        await _cleanup(r, auth)


async def test_no_cost_limit_reserves_and_settles_nothing(real_redis, monkeypatch):
    """비용 한도가 어디에도 없으면 예약도 정산도 없다 — 이 요청의 rl:cost 키는 0 개."""
    r = real_redis
    auth = _auth()
    try:
        state, keys = await _reserve(monkeypatch, r, auth, user=False, team=False)
        assert keys == set()
        assert state["rate_limit_state"]["cost_reserved"] == Decimal("0")
        assert state["rate_limit_state"].get("cost_reserved_keys", ()) == ()
        await _finalize(r, auth, state, _SMALL_USAGE)
        keyspace = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(keyspace)
        assert _of(keyspace, auth) == {}
    finally:
        await _cleanup(r, auth)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 창 — 경계를 넘긴 정산, 만료된 예약 키
# ─────────────────────────────────────────────────────────────────────────────


async def test_settle_after_minute_and_hour_boundary_stays_in_the_reserved_windows(
    real_redis, monkeypatch
):
    """59분 59.5초에 예약, 다음 시 0분 0.5초에 정산 → 예약한 (분, 시) 창의 키만 조정된다.

    옛 정산은 정산 시각으로 창을 골라 **다음** 분·시 창에 음수·TTL −1 키를 새로 만들고, 예약한 창의
    예약은 그대로 남겼다. 시계는 rate_limit_service 의 ``time`` 만 바꾼다(키 이름의 window_ts).
    TTL 은 실제 Redis 시간이다.
    """
    from app.services import rate_limit_service as rls

    r = real_redis
    auth = _auth()
    boundary = (int(time.time()) // 3600 + 1) * 3600
    clock = [boundary - 0.5]
    monkeypatch.setattr(rls, "time", SimpleNamespace(time=lambda: clock[0]))
    try:
        state, keys = await _reserve(monkeypatch, r, auth, user=True, team=True)
        assert len(keys) == 4
        assert all(k.endswith((f":cpm:{boundary - 60}", f":cph:{boundary - 3600}")) for k in keys)

        clock[0] = boundary + 0.5  # 분과 시가 모두 바뀌었다
        actual = float(calculate_cost(_SMALL_USAGE, _model()))
        await _finalize(r, auth, state, _SMALL_USAGE)

        keyspace = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(keyspace)
        after = _of(keyspace, auth)
        assert set(after) == keys, f"다음 창에 키가 생겼다: {sorted(set(after) - keys)}"
        assert all(v[0] == pytest.approx(actual, abs=1e-12) for v in after.values()), after
    finally:
        await _cleanup(r, auth)


@pytest.mark.parametrize("crossing", ["minute", "hour"])
async def test_an_extra_charge_after_the_reserved_window_lands_in_the_current_window(
    real_redis, monkeypatch, crossing
):
    """실제 > 예약인데 정산 때 예약한 창이 이미 지났다(분 경계, 또는 분·시 경계) → 추가분(실제 −
    예약)은 같은 스코프의 **지금** 창 키에 간다(새로 만들고 TTL 을 건다). 예약한 창의 키는 예약액
    그대로다. 지나지 않은 창(분 경계에서의 cph)은 예약한 키가 실제 비용이 된다. 예약하지 않은
    스코프(USER)에는 키가 생기지 않는다.

    예전에는 추가분이 지난 창의 키에 더해졌다 — 그 키는 어떤 검사도 읽지 않으므로 그 비용은 지금
    창의 한도에서 빠졌다. 환불은 그대로 예약한 창에서 한다(위 경계 테스트).
    """
    from app.services import rate_limit_service as rls

    r = real_redis
    auth = _auth()
    big = TokenUsage(input_tokens=50, output_tokens=4000, total_tokens=4050)
    hour = (int(time.time()) // 3600 + 1) * 3600
    boundary = hour if crossing == "hour" else hour + 600  # hour + 600: 시 안의 분 경계
    clock = [boundary - 0.5]
    monkeypatch.setattr(rls, "time", SimpleNamespace(time=lambda: clock[0]))
    team = f"rl:cost:team:{{{auth.team_id}}}"
    cpm_old, cph_old = f"{team}:cpm:{boundary - 60}", f"{team}:cph:{(boundary - 1) // 3600 * 3600}"
    cpm_now, cph_now = f"{team}:cpm:{boundary}", f"{team}:cph:{boundary // 3600 * 3600}"
    try:
        state, keys = await _reserve(monkeypatch, r, auth, user=False, team=True, max_tokens=10)
        assert keys == {cpm_old, cph_old}
        reserved = _reserved(state)
        actual = float(calculate_cost(big, _model()))
        assert actual > reserved

        clock[0] = boundary + 0.5
        await _finalize(r, auth, state, big)

        keyspace = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(keyspace)
        after = _of(keyspace, auth)
        extra = actual - reserved
        if crossing == "hour":
            expect = {cpm_old: reserved, cph_old: reserved, cpm_now: extra, cph_now: extra}
        else:
            assert cph_now == cph_old
            expect = {cpm_old: reserved, cph_old: actual, cpm_now: extra}
        assert {k: v[0] for k, v in after.items()} == pytest.approx(expect, abs=1e-12)
        assert 0 < after[cpm_now][1] <= 120
        assert 0 < after[cph_now][1] <= 7200
    finally:
        await _cleanup(r, auth)


async def _expire_now(r, keys) -> None:
    """예약 키를 진짜 TTL 만료로 없앤다(DEL 이 아니라 만료 — 스트림이 cpm TTL 120s 를 넘긴 경우)."""
    for key in keys:
        await r.pexpire(key, 1)
    await asyncio.sleep(0.05)
    for key in keys:
        assert not await r.exists(key), f"{key} 가 만료되지 않았다"


async def test_settle_after_the_reserved_cpm_key_expired_does_not_resurrect_it(
    real_redis, monkeypatch
):
    """cpm 키(TTL 120s)가 만료된 뒤 정산(환불 방향) → cpm 키는 다시 생기지 않고 cph 만 조정된다.

    만료됐다는 것은 그 분 창이 이미 끝났다는 뜻이다(예약마다 TTL 을 창의 두 배로 다시 건다).
    옛 정산은 정산 시각의 cpm 키를 INCRBYFLOAT 해 음수·TTL −1 로 되살렸다.
    """
    r = real_redis
    auth = _auth()
    await _clear_of_window_edge()
    try:
        state, keys = await _reserve(monkeypatch, r, auth, user=True, team=True)
        cpm_keys = {k for k in keys if ":cpm:" in k}
        assert len(cpm_keys) == 2
        await _expire_now(r, cpm_keys)

        actual = float(calculate_cost(_SMALL_USAGE, _model()))
        await _finalize(r, auth, state, _SMALL_USAGE)

        keyspace = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(keyspace)
        after = _of(keyspace, auth)
        assert set(after) == keys - cpm_keys, (
            f"만료된 키가 되살아났다: {sorted(set(after) & cpm_keys)}"
        )
        assert all(v[0] == pytest.approx(actual, abs=1e-12) for v in after.values()), after
    finally:
        await _cleanup(r, auth)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 정산 방향 — zero-usage 환불, 실제 > 예약(추가 청구)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "user,team", [(True, False), (False, True)], ids=["user-only", "team-only"]
)
async def test_zero_usage_refund_returns_the_reserved_counters_to_exactly_zero(
    real_redis, monkeypatch, user, team
):
    """응답을 한 토큰도 못 받은 요청(finalize 의 zero-usage 경로) → 예약한 키 = 정확히 0.0."""
    r = real_redis
    auth = _auth()
    await _clear_of_window_edge()
    try:
        state, keys = await _reserve(monkeypatch, r, auth, user=user, team=team)
        assert len(keys) == 2
        await _finalize(r, auth, state, TokenUsage())

        keyspace = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(keyspace)
        after = _of(keyspace, auth)
        assert set(after) == keys, (
            f"예약하지 않은 스코프에 키가 생겼다: {sorted(set(after) - keys)}"
        )
        assert all(v[0] == 0.0 for v in after.values()), f"환불이 정확히 되돌리지 않았다: {after}"
    finally:
        await _cleanup(r, auth)


async def test_extra_charge_lands_in_the_reserved_window_and_never_resurrects_an_expired_key(
    real_redis, monkeypatch
):
    """실제 비용 > 예약(max_tokens 가 작고 출력이 큼) → 예약한 창의 키가 실제 비용이 된다.

    두 요청 중 둘째는 cpm 키가 만료된 뒤 정산한다 — 추가 청구라도 만료된 창의 키는 만들지 않는다
    (그 창을 읽을 검사가 없다). 팀 한도만 걸어, 옛 정산이 예약 없는 USER 스코프에 양수·TTL −1 키를
    만들던 것도 같이 본다.
    """
    r = real_redis
    big = TokenUsage(input_tokens=50, output_tokens=4000, total_tokens=4050)
    live, expired = _auth(), _auth()
    await _clear_of_window_edge()
    try:
        s_live, k_live = await _reserve(monkeypatch, r, live, user=False, team=True, max_tokens=10)
        s_exp, k_exp = await _reserve(monkeypatch, r, expired, user=False, team=True, max_tokens=10)
        actual = float(calculate_cost(big, _model()))
        assert actual > _reserved(s_live)
        exp_cpm = {k for k in k_exp if ":cpm:" in k}
        assert len(exp_cpm) == 1
        await _expire_now(r, exp_cpm)

        await _finalize(r, live, s_live, big)
        await _finalize(r, expired, s_exp, big)

        keyspace = await _cost_keyspace(r, live, expired)
        _assert_no_ttl_less_or_negative(keyspace)
        after_live = _of(keyspace, live)
        assert set(after_live) == k_live, sorted(after_live)
        assert all(v[0] == pytest.approx(actual, abs=1e-12) for v in after_live.values())
        after_exp = _of(keyspace, expired)
        assert set(after_exp) == k_exp - exp_cpm, sorted(after_exp)
        assert all(v[0] == pytest.approx(actual, abs=1e-12) for v in after_exp.values())
    finally:
        await _cleanup(r, live)
        await _cleanup(r, expired)


# ─────────────────────────────────────────────────────────────────────────────
# 4. release_reservations (폴백 루프 · 비-2xx) 와 옛 모양의 정산 상태
# ─────────────────────────────────────────────────────────────────────────────


async def test_fallback_release_undoes_only_the_reserved_scope_exactly_once(
    real_redis, monkeypatch
):
    """``release_reservations`` → 예약한 키 = 0.0. 그 뒤 미리 잡아 둔 사본으로 finalize 가 돌아도
    (openai_compat 스트리밍 경로) 다시 환불하지 않는다. 예약하지 않은 TEAM 키는 끝까지 없다."""
    r = real_redis
    auth = _auth()
    await _clear_of_window_edge()
    try:
        state, keys = await _reserve(monkeypatch, r, auth, user=True, team=False)
        assert len(keys) == 2
        captured = state["rate_limit_state"]

        await release_reservations(redis=r, state=state, auth_context=auth)
        mid = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(mid)
        assert set(_of(mid, auth)) == keys
        assert all(v[0] == 0.0 for v in _of(mid, auth).values()), _of(mid, auth)

        await CostRecorder().finalize(
            r, auth, _model(), TokenUsage(), "req-captured", True, 1, rate_limit_state=captured
        )
        keyspace = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(keyspace)
        after = _of(keyspace, auth)
        assert set(after) == keys
        assert all(v[0] == 0.0 for v in after.values()), f"이중 환불: {after}"
    finally:
        await _cleanup(r, auth)


async def test_legacy_state_without_reserved_keys_writes_no_cost_key(real_redis, monkeypatch):
    """``cost_reserved_keys`` 가 없는(이 필드 이전 모양의) 정산 상태 → 비용 키를 하나도 쓰지 않는다.

    어느 스코프의 어느 창을 예약했는지 모르는 채로 쓰면 그것이 곧 이 결함이다. 대가는 그 예약이
    창이 끝날 때까지 남는 것(과잉제한, cpm 최대 1분·cph 최대 1시간)이고, 경고 로그로 드러난다.
    """
    r = real_redis
    auth = _auth()
    await _clear_of_window_edge()
    try:
        state, keys = await _reserve(monkeypatch, r, auth, user=True, team=False)
        assert len(keys) == 2
        reserved = _reserved(state)
        state["rate_limit_state"].pop("cost_reserved_keys", None)

        with capture_logs() as logs:
            await _finalize(r, auth, state, _SMALL_USAGE)

        keyspace = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(keyspace)
        after = _of(keyspace, auth)
        assert set(after) == keys, (
            f"예약하지 않은 스코프에 키가 생겼다: {sorted(set(after) - keys)}"
        )
        assert all(v[0] == pytest.approx(reserved) for v in after.values()), after
        assert [e["event"] for e in _settle_failures(logs)] == [
            "cost_settle_skipped_no_reserved_keys"
        ]
    finally:
        await _cleanup(r, auth)


# ─────────────────────────────────────────────────────────────────────────────
# 5. 클러스터 — 두 스코프를 한 EVAL 에 넣으면 거부된다(정산이 스코프별 EVAL 인 이유)
# ─────────────────────────────────────────────────────────────────────────────


async def test_cluster_rejects_a_two_scope_settle(cluster_redis, monkeypatch):
    """USER·TEAM 예약 키는 클러스터에서 슬롯이 다르고, 둘을 한 EVAL 로 보내면 **서버가** 거부한다.

    그래서 정산은 스코프마다 EVAL 을 한 번씩 한다. 위 시나리오들이 클러스터에서 값까지 맞는 것이
    그 분리가 실제로 동작한다는 증거이고, 이 테스트는 합쳤을 때의 실패가 진짜임을 보인다.
    """
    from redis.exceptions import ResponseError

    r = cluster_redis
    auth = _auth()
    await _clear_of_window_edge()
    try:
        state, keys = await _reserve(monkeypatch, r, auth, user=True, team=True)
        u_cpm, u_cph, t_cpm, t_cph = (
            next(k for k in keys if k.startswith(f"rl:cost:{scope}:") and f":{kind}:" in k)
            for scope in ("user", "team")
            for kind in ("cpm", "cph")
        )
        assert r.keyslot(u_cpm) == r.keyslot(u_cph)
        assert r.keyslot(t_cpm) == r.keyslot(t_cph)
        assert r.keyslot(u_cpm) != r.keyslot(t_cpm)

        script = LuaScriptLoader.get("cost_settle_scope")
        node = r.get_node_from_key(u_cpm)
        with pytest.raises(ResponseError, match="hash to the same slot"):
            # 클라이언트의 슬롯 검사를 건너뛰고 서버에 직접 보낸다 — 서버의 CROSSSLOT 거부를 본다.
            await r.execute_command(
                "EVAL", script, 2, u_cpm, t_cpm, "-0.001", target_nodes=node
            )
        # 거부된 EVAL 은 아무것도 쓰지 않았다.
        for key in (u_cpm, u_cph, t_cpm, t_cph):
            assert float(_text(await r.get(key))) == pytest.approx(_reserved(state)), key
    finally:
        await _cleanup(r, auth)


class _CounterSpy:
    """OTel Counter 대역 — add(1, {"kind": ...}) 를 센다(정산 이상 카운터 배선 확인용)."""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    def add(self, n, attrs=None) -> None:
        self.kinds.extend([(attrs or {}).get("kind")] * n)


async def test_a_negative_counter_left_by_the_old_settlement_is_clamped_and_counted(
    real_redis, monkeypatch
):
    """이 수정 이전 정산이 지금 창에 남긴 음수·TTL 없는 키(−0.05) 위에서 예약하고 환불하면,
    결과가 음수가 되는 대신 0 이 되고 TTL 을 갖는다 — 그리고 그 0 하한은 **조용하지 않다**:
    경고 로그와 ``gateway_rl_settle_clamped_total{kind=cost}`` 가 남는다. 남은 음수는 그 창
    동안 남의 한도를 빌려 준다.
    """
    from app.services import rate_limit_service as rls

    r = real_redis
    auth = _auth()
    clamped, duplicate = _CounterSpy(), _CounterSpy()
    rls.set_settle_metrics(clamped=clamped, duplicate=duplicate)
    await _clear_of_window_edge()
    now = int(time.time())
    legacy = [
        f"rl:cost:user:{{{auth.user_id}}}:cpm:{(now // 60) * 60}",
        f"rl:cost:user:{{{auth.user_id}}}:cph:{(now // 3600) * 3600}",
    ]
    try:
        for key in legacy:
            await r.set(key, "-0.05")  # 옛 정산이 남긴 모양: 음수, TTL 없음
        state, keys = await _reserve(monkeypatch, r, auth, user=True, team=False)
        assert keys == set(legacy)
        with capture_logs() as logs:
            await _finalize(r, auth, state, TokenUsage())

        keyspace = await _cost_keyspace(r, auth)
        _assert_no_ttl_less_or_negative(keyspace)
        assert all(v[0] == 0.0 for v in _of(keyspace, auth).values()), _of(keyspace, auth)
        assert [e["event"] for e in logs if e.get("event") == "cost_settle_clamped"] == [
            "cost_settle_clamped"
        ]
        assert clamped.kinds == ["cost"] and duplicate.kinds == []
    finally:
        rls.set_settle_metrics(clamped=None, duplicate=None)
        await _cleanup(r, auth)


@pytest.mark.parametrize("first", ["partial", "exact"], ids=["partial-refund", "zero-adjustment"])
async def test_a_second_settle_of_the_same_reservation_is_a_reported_no_op(
    real_redis, monkeypatch, first
):
    """같은 예약(같은 reservation_id)을 두 번 정산한다 — 결함이 보낼 두 번째 정산이다(정산 상태의
    사본, 늦은 응답의 재시도). 같은 창 키에 다른 요청(bystander)의 예약이 함께 있다.

    두 번째 정산은 스크립트의 정산 표지(SET NX)에 막혀 아무것도 쓰지 않는다: 키 = 다른 요청의 예약
    + 이 요청의 실제 비용, 정확히. 그리고 조용하지 않다 — ``cost_settle_duplicate`` 경고와
    ``gateway_rl_settle_duplicate_total{kind=cost}`` 가 한 번. ``zero-adjustment``: 첫 정산의 차액이
    0(실제 == 예약)이어도 스크립트가 돌아 표지를 세운다 — 그래서 뒤따른 전액 환불(결함)도 막힌다.
    """
    from app.services import rate_limit_service as rls
    from app.services.rate_limit_service import RateLimitService

    r = real_redis
    auth = _auth()
    clamped, duplicate = _CounterSpy(), _CounterSpy()
    rls.set_settle_metrics(clamped=clamped, duplicate=duplicate)
    await _clear_of_window_edge()
    try:
        state_a, keys = await _reserve(monkeypatch, r, auth, user=True, team=False)
        state_b, _ = await _reserve(monkeypatch, r, auth, user=True, team=False)  # bystander
        a, b = state_a["rate_limit_state"], state_b["rate_limit_state"]
        reserved_a = a["cost_reserved"]
        actual_a = reserved_a / 2 if first == "partial" else reserved_a
        settles = [actual_a, actual_a] if first == "partial" else [actual_a, Decimal("0")]
        with capture_logs() as logs:
            for actual in settles:
                await RateLimitService().settle_cost(
                    r, user_id=auth.user_id, actual_cost=actual, reserved_cost=reserved_a,
                    team_id=auth.team_id, reserved_keys=a["cost_reserved_keys"],
                    reservation_id=a["reservation_id"],
                )
        after = _of(await _cost_keyspace(r, auth), auth)
        expect = float(b["cost_reserved"] + actual_a)
        assert set(after) == keys
        exact = [v[0] == pytest.approx(expect, abs=1e-12) for v in after.values()]
        assert all(exact), (expect, after)
        events = [e["event"] for e in logs if e["event"].startswith("cost_settle_")]
        assert events == ["cost_settle_duplicate"], events
        assert duplicate.kinds == ["cost"] and clamped.kinds == []
    finally:
        rls.set_settle_metrics(clamped=None, duplicate=None)
        await _cleanup(r, auth)
