# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""두 개의 조용한 Redis 수명 결함 — 엉뚱한 창에 얹히는 환불, 그리고 "$0 / $0" 응답.

결함 1 — 정산이 예약한 창으로 돌아가지 않았다
---------------------------------------------
CPM/CPH 예약은 ``rl:cost:<scope>:{id}:<cpm|cph>:<window_ts>`` 에 들어간다. 창 타임스탬프가
**키 이름의 일부**다. 그런데 ``settle_cost`` 는 정산 시점의 ``now`` 로 창을 다시 계산했다.

예약은 ``max_tokens`` 기준 과대 추정이므로 차액은 거의 항상 **음수(환불)** 다. 요청이 분
경계를 넘기면(60초를 넘는 스트리밍은 매번 그렇다) 그 환불이 **다음 창**에 얹혔다:

  * 다음 창은 이 요청이 소비한 적이 없다 → 사용자는 쓰지 않은 헤드룸을 받는다.
  * 예약이 실제로 들어간 창은 과대 예약을 그대로 안고 남는다 → 그 창의 남은 시간 동안
    실제보다 빨리 429 를 맞는다.

한 요청이 두 창을 서로 반대 방향으로 왜곡한다.

그리고 ``INCRBYFLOAT`` 는 **없는 키를 만든다**. 그렇게 만들어진 키에는 TTL 이 없다.
사용자·분 단위 키이므로, 경계를 넘는 요청마다 불멸의 키가 하나씩 쌓였다.

같은 함수에 세 번째 결함이 있었다: ``reserve_cost`` 는 USER 통과 → TEAM 거부 시 USER
카운터를 되돌리지 않았고, 거부 응답의 ``reserved_cost`` 는 0 이라 **하류의 어떤 정산도
그 예약을 알지 못했다**.

결함 2 — 예산 캐시가 만료되면 사용량 응답이 "$0 / $0" 이었다
------------------------------------------------------------
``budget:config:user:{uid}`` 는 TTL 300초다. 만료는 예외가 아니라 정상 동작이다. 그런데
``/v1/usage/me`` 는 그 키가 없으면 예산 블록 전체를 건너뛰어 max/used/remaining 이 모두
0 인 기본값을 응답에 실었다 — 그리고 ``used`` 마저 config 가 있을 때만 읽었으므로 사용액
키에 진짜 값이 있어도 함께 버려졌다.

5분 넘게 요청하지 않은 사용자가 CLI statusline 을 열면 "한도 없음, 지출 없음" 을 봤다.

⚠️ 검증은 **실 Redis + 실 PostgreSQL** 로 한다. 창 수명(TTL), 키 부활, 만료된 캐시 뒤의
   DB 폴백은 모두 저장소의 실제 성질이고, 가짜로는 그 성질 자체가 존재하지 않는다.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.rate_limit_service import RateLimitService

REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")
PROOF_DSN = os.environ.get("PROOF_DSN")

_redis_required = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason=(
        "REDIS_PROOF_URL 미설정 — 실 Redis 필요. TTL 과 키 부활은 저장소의 성질이라 "
        "가짜에는 그 성질이 없다."
    ),
)
_pg_required = pytest.mark.skipif(
    not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PG 필요(예산 DB 폴백)."
)


@pytest.fixture(autouse=True)
def _load_lua_scripts():
    """Lua 는 앱 부팅 시 로드된다 — 테스트는 그 부팅을 거치지 않으므로 직접 읽는다.

    안 하면 ``KeyError: not loaded`` 가 나고, 그 실패는 "정산이 동작하지 않는다" 와
    구별되지 않는다.
    """
    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader.load_all(
        Path(__file__).resolve().parents[2] / "src" / "app" / "redis_scripts"
    )


def _keys(prefix: str, sid: str, cpm_ts: int, cph_ts: int) -> tuple[str, str]:
    return (
        f"rl:cost:{prefix}:{{{sid}}}:cpm:{cpm_ts}",
        f"rl:cost:{prefix}:{{{sid}}}:cph:{cph_ts}",
    )


def _windows(now: int | None = None) -> tuple[int, int]:
    now = now if now is not None else int(time.time())
    return (now // 60) * 60, (now // 3600) * 3600


# ─────────────────────────────────────────────────────────────────────────────
# 1. 정산은 예약한 창으로 돌아간다
# ─────────────────────────────────────────────────────────────────────────────


@_redis_required
async def test_a_refund_lands_in_the_window_the_reservation_was_made_in():
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = str(uuid.uuid4())
    cpm_ts, cph_ts = _windows()
    cpm_key, cph_key = _keys("user", uid, cpm_ts, cph_ts)
    svc = RateLimitService()
    try:
        res = await svc.reserve_cost(
            r,
            user_id=uid,
            estimated_cost=Decimal("1.00"),
            user_cpm_limit=Decimal("10"),
            user_cph_limit=Decimal("100"),
        )
        assert res.allowed, "예약이 거부됐다 — 전제가 깨졌다"
        assert res.cpm_window_ts == cpm_ts, (
            f"예약이 창을 돌려주지 않는다: {res.cpm_window_ts} != {cpm_ts}"
        )
        assert float(await r.get(cpm_key)) == pytest.approx(1.00)

        # 실제 비용 0.10 → 0.90 환불
        await svc.settle_cost(
            r,
            user_id=uid,
            actual_cost=Decimal("0.10"),
            reserved_cost=Decimal("1.00"),
            cpm_window_ts=res.cpm_window_ts,
            cph_window_ts=res.cph_window_ts,
        )
        assert float(await r.get(cpm_key)) == pytest.approx(0.10), (
            "예약한 창에 환불이 반영되지 않았다"
        )
        assert float(await r.get(cph_key)) == pytest.approx(0.10)
    finally:
        await r.delete(cpm_key, cph_key)
        await r.aclose()


@_redis_required
async def test_a_refund_for_a_window_that_has_rolled_does_not_credit_the_new_window():
    """⚠️ 이 파일의 핵심. 경계를 넘긴 요청의 환불이 **다음 창**에 얹히면 안 된다.

    다음 창은 이 요청이 소비한 적이 없다 — 거기에 음수를 넣는 것은 정산이 아니라 다른
    창에 대한 할인이다.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = str(uuid.uuid4())
    now = int(time.time())
    cur_cpm, cur_cph = _windows(now)
    # 예약은 5분 전 창에서 일어났다고 본다(그 키는 이미 만료돼 존재하지 않는다).
    old_cpm = cur_cpm - 300
    old_cph = cur_cph
    old_key, _ = _keys("user", uid, old_cpm, old_cph)
    cur_key, _ = _keys("user", uid, cur_cpm, cur_cph)
    svc = RateLimitService()
    try:
        assert await r.exists(old_key) == 0, "전제: 지난 창의 키는 없다"
        # ⚠️ 현재 창에는 **다른 요청의 예약**이 이미 들어 있다. 이 설정이 없으면 창을
        #    잘못 계산해도 EXISTS 가드에 걸려 결과가 같아지고, 이 테스트는 오조준을
        #    구별하지 못한다(대조군으로 확인: 창 재계산을 되살려도 통과했다).
        await r.set(cur_key, "3.0", ex=120)
        await svc.settle_cost(
            r,
            user_id=uid,
            actual_cost=Decimal("0"),
            reserved_cost=Decimal("1.00"),
            cpm_window_ts=old_cpm,
            cph_window_ts=old_cph,
        )
        assert await r.exists(old_key) == 0, (
            "지난 창의 키를 되살렸다 — INCRBYFLOAT 는 없는 키를 만들고, 그 키엔 TTL 이 없다"
        )
        assert float(await r.get(cur_key)) == pytest.approx(3.0), (
            f"환불이 현재 창에 얹혔다({await r.get(cur_key)}) — 이 요청이 소비한 적 없는 "
            "창에 헤드룸을 만들어 준다"
        )
    finally:
        await r.delete(old_key, cur_key)
        await r.aclose()


@_redis_required
async def test_a_settle_for_a_current_window_does_not_create_a_key_from_nothing():
    """⚠️ EXISTS 가드를 **단독으로** 검증한다.

    창이 지난 경우는 TTL≤0 가드에도 걸리므로, 그 경로만 보면 EXISTS 가드를 지워도
    테스트가 통과한다(대조군으로 확인했다). 창이 현재인데 키가 없는 상태 — 예약이
    0 이었거나 키가 이미 축출된 경우 — 에서는 EXISTS 만이 막는다.

    없는 키에 음수를 넣으면 그 사용자는 다음 요청에서 공짜 헤드룸을 얻는다.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = str(uuid.uuid4())
    cpm_ts, cph_ts = _windows()
    cpm_key, cph_key = _keys("user", uid, cpm_ts, cph_ts)
    try:
        assert await r.exists(cpm_key) == 0, "전제: 키가 없다"
        await RateLimitService().settle_cost(
            r,
            user_id=uid,
            actual_cost=Decimal("0"),
            reserved_cost=Decimal("1.00"),
            cpm_window_ts=cpm_ts,   # 현재 창 → TTL 가드는 통과한다
            cph_window_ts=cph_ts,
        )
        assert await r.exists(cpm_key) == 0, (
            "없는 키를 만들었다 — 다음 요청이 공짜 헤드룸을 얻는다"
        )
        assert await r.exists(cph_key) == 0
    finally:
        await r.delete(cpm_key, cph_key)
        await r.aclose()


@_redis_required
async def test_every_key_the_settle_touches_still_has_a_ttl():
    """⚠️ TTL 없는 ``rl:cost:*`` 키는 영구히 남는다 — 사용자·분 단위라 무한히 늘어난다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = str(uuid.uuid4())
    cpm_ts, cph_ts = _windows()
    cpm_key, cph_key = _keys("user", uid, cpm_ts, cph_ts)
    svc = RateLimitService()
    try:
        await svc.reserve_cost(
            r,
            user_id=uid,
            estimated_cost=Decimal("1.00"),
            user_cpm_limit=Decimal("10"),
            user_cph_limit=Decimal("100"),
        )
        await svc.settle_cost(
            r,
            user_id=uid,
            actual_cost=Decimal("0.10"),
            reserved_cost=Decimal("1.00"),
            cpm_window_ts=cpm_ts,
            cph_window_ts=cph_ts,
        )
        for k in (cpm_key, cph_key):
            ttl = await r.ttl(k)
            assert ttl > 0, f"{k} 의 TTL 이 {ttl} — 영구 키가 됐다"
    finally:
        await r.delete(cpm_key, cph_key)
        await r.aclose()


@_redis_required
async def test_a_legacy_key_without_a_ttl_gets_one_when_settled():
    """과거 결함으로 이미 만들어진 불멸의 키는 이 경로를 한 번 타면 회수된다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = str(uuid.uuid4())
    cpm_ts, cph_ts = _windows()
    cpm_key, cph_key = _keys("user", uid, cpm_ts, cph_ts)
    svc = RateLimitService()
    try:
        await r.set(cpm_key, "5.0")  # TTL 없이 — 과거 결함이 만든 형태
        assert await r.ttl(cpm_key) == -1, "전제: TTL 이 없다"
        await svc.settle_cost(
            r,
            user_id=uid,
            actual_cost=Decimal("0"),
            reserved_cost=Decimal("1.00"),
            cpm_window_ts=cpm_ts,
            cph_window_ts=cph_ts,
        )
        assert await r.ttl(cpm_key) > 0, "TTL 이 붙지 않았다 — 키가 계속 영구히 남는다"
        assert float(await r.get(cpm_key)) == pytest.approx(4.0)
    finally:
        await r.delete(cpm_key, cph_key)
        await r.aclose()


@_redis_required
async def test_a_team_rejection_refunds_the_user_scope_it_already_charged():
    """⚠️ USER 통과 → TEAM 거부. 거부 응답의 reserved_cost 는 0 이므로 하류는 모른다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    cpm_ts, cph_ts = _windows()
    ucpm, ucph = _keys("user", uid, cpm_ts, cph_ts)
    tcpm, tcph = _keys("team", tid, cpm_ts, cph_ts)
    svc = RateLimitService()
    try:
        res = await svc.reserve_cost(
            r,
            user_id=uid,
            estimated_cost=Decimal("5.00"),
            user_cpm_limit=Decimal("100"),
            user_cph_limit=Decimal("1000"),
            team_id=tid,
            team_cpm_limit=Decimal("1"),  # 팀은 한 건도 못 받는다
            team_cph_limit=Decimal("1000"),
        )
        assert not res.allowed, "TEAM 한도에 걸리지 않았다 — 전제가 깨졌다"
        assert res.scope == "TEAM"

        user_cpm_val = float(await r.get(ucpm) or 0)
        assert user_cpm_val == pytest.approx(0.0), (
            f"사용자 CPM 에 {user_cpm_val} 이 남았다 — 서빙되지 않은 요청이 창이 끝날 "
            "때까지 사용자의 비용 한도를 물고 있다"
        )
        assert float(await r.get(ucph) or 0) == pytest.approx(0.0)
    finally:
        await r.delete(ucpm, ucph, tcpm, tcph)
        await r.aclose()


@_redis_required
async def test_a_passing_reservation_still_consumes_the_counter():
    """⚠️ 대조군. 위 단정들이 "항상 0" 으로 통과하는 것이 아님을 보인다.

    이것이 없으면 예약 자체를 없애도 위 테스트들이 통과한다.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    cpm_ts, cph_ts = _windows()
    ucpm, ucph = _keys("user", uid, cpm_ts, cph_ts)
    tcpm, tcph = _keys("team", tid, cpm_ts, cph_ts)
    svc = RateLimitService()
    try:
        res = await svc.reserve_cost(
            r,
            user_id=uid,
            estimated_cost=Decimal("2.00"),
            user_cpm_limit=Decimal("100"),
            user_cph_limit=Decimal("1000"),
            team_id=tid,
            team_cpm_limit=Decimal("100"),
            team_cph_limit=Decimal("1000"),
        )
        assert res.allowed
        assert float(await r.get(ucpm)) == pytest.approx(2.0), "통과했는데 예약이 안 잡혔다"
        assert float(await r.get(tcpm)) == pytest.approx(2.0)
    finally:
        await r.delete(ucpm, ucph, tcpm, tcph)
        await r.aclose()


@_redis_required
async def test_settle_never_raises_when_redis_is_down():
    """정산 실패로 요청 처리를 바꿀 이유는 없다."""

    class _Dead:
        async def eval(self, *a, **k):
            raise RuntimeError("redis down")

        async def evalsha(self, *a, **k):
            raise RuntimeError("redis down")

    await RateLimitService().settle_cost(
        _Dead(),
        user_id="u",
        actual_cost=Decimal("0"),
        reserved_cost=Decimal("1"),
        team_id="t",
        cpm_window_ts=0,
        cph_window_ts=0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. /v1/usage/me — 캐시가 만료돼도 실제 예산을 보여준다
# ─────────────────────────────────────────────────────────────────────────────

USER_ID = "61111111-1111-1111-1111-111111111111"
TEAM_ID = "62222222-2222-2222-2222-222222222222"
DEPT_ID = "63333333-3333-3333-3333-333333333333"
ORG_ID = "64444444-4444-4444-4444-444444444444"
_OWNED_SUFFIX = "_budgetvis"


async def _make_db() -> str:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, _, name = PROOF_DSN.rpartition("/")
    owned = f"{name}{_OWNED_SUFFIX}"
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await c.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            await c.execute(text(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"'))
    finally:
        await admin.dispose()
    return f"{prefix}/{owned}"


@pytest.fixture(scope="module")
async def budget_db():
    asyncpg = pytest.importorskip("asyncpg")
    if not PROOF_DSN:
        pytest.skip("PROOF_DSN 미설정")
    url = await _make_db()
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute(
            "INSERT INTO auth.organizations (id, name) VALUES ($1,'bv-org') "
            "ON CONFLICT (id) DO NOTHING",
            ORG_ID,
        )
        await conn.execute(
            "INSERT INTO auth.departments (id, org_id, name) VALUES ($1,$2,'bv-dept') "
            "ON CONFLICT (id) DO NOTHING",
            DEPT_ID,
            ORG_ID,
        )
        await conn.execute(
            "INSERT INTO auth.teams (id, dept_id, name) VALUES ($1,$2,'bv-team') "
            "ON CONFLICT (id) DO NOTHING",
            TEAM_ID,
            DEPT_ID,
        )
        await conn.execute(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES ($1,$2,'bv@example.invalid','bv','ADMIN','bv-sub') "
            "ON CONFLICT (id) DO NOTHING",
            USER_ID,
            TEAM_ID,
        )
        # 사용자 전체 예산 $50, per-app 예산 $3 (이것이 전체로 오인되면 안 된다)
        await conn.execute(
            "INSERT INTO budget.budget_configs "
            "(scope, scope_id, client, max_budget_usd, period_type, policy, allocated_by, "
            " effective_from, is_active) "
            "VALUES ('USER',$1,NULL,50.0000,'MONTHLY','SOFT_WARNING',$1,'2026-01-01',true)",
            USER_ID,
        )
        await conn.execute(
            "INSERT INTO budget.budget_configs "
            "(scope, scope_id, client, max_budget_usd, period_type, policy, allocated_by, "
            " effective_from, is_active) "
            "VALUES ('USER',$1,'codex',3.0000,'MONTHLY','HARD_BLOCK',$1,'2026-01-01',true)",
            USER_ID,
        )
    finally:
        await conn.close()
    yield url

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, _, name = PROOF_DSN.rpartition("/")
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(
                text(f'DROP DATABASE IF EXISTS "{name}{_OWNED_SUFFIX}" WITH (FORCE)')
            )
    finally:
        await admin.dispose()


class _Req:
    """``usage_me`` 가 실제로 읽는 것만 담은 요청 스텁."""

    def __init__(self, state: dict):
        self.scope = {"state": state}


class _Auth:
    user_id = USER_ID
    team_id = TEAM_ID
    dept_id = DEPT_ID
    sso_subject = "bv-sub"
    role = None


async def _call_usage_me(redis, session_factory, period: str) -> dict:
    from app.routers.usage import usage_me

    resp = await usage_me(
        _Req({"auth_context": _Auth(), "_redis": redis, "_session_factory": session_factory}),
        period=period,
    )
    return json.loads(bytes(resp.body))


def _factory(url: str):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    return async_sessionmaker(create_async_engine(url), expire_on_commit=False)


@_pg_required
@_redis_required
async def test_an_expired_config_cache_no_longer_reports_zero_of_zero(budget_db):
    """⚠️ 이 결함의 결론. config 키가 없어도 실제 한도와 실제 사용액이 나와야 한다.

    그 키는 TTL 300초다 — 없는 상태가 정상이다.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    period = "2026-06"
    factory = _factory(budget_db)
    try:
        await r.delete(f"budget:config:user:{{{USER_ID}}}")
        await r.delete(f"budget:user:{{{USER_ID}}}:{period}")
        # 워커가 커밋한 월 사용액
        async with factory() as s:
            from sqlalchemy import text

            await s.execute(
                text(
                    "INSERT INTO budget.budget_usages "
                    "(scope, scope_id, client, period, used_usd, limit_usd) "
                    "VALUES ('USER', :u, NULL, :p, 12.5000, 50.0000) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"u": USER_ID, "p": period},
            )
            await s.commit()

        body = await _call_usage_me(r, factory, period)
        b = body["budget"]
        assert Decimal(b["max_usd"]) == Decimal("50.0000"), (
            f"한도가 {b['max_usd']} — 캐시가 만료되면 0 을 보고했던 그 결함이다"
        )
        assert Decimal(b["used_usd"]) == Decimal("12.5000"), (
            f"사용액이 {b['used_usd']} — config 가 없으면 used 까지 버렸던 그 결함이다"
        )
        assert Decimal(b["remaining_usd"]) == Decimal("37.5000")
        assert b["pct"] == pytest.approx(25.0)
        assert b["policy"] == "soft_warning", (
            f"정책이 {b['policy']} — DB enum 라벨이 소문자로 변환되지 않았다"
        )
    finally:
        await r.aclose()


@_pg_required
@_redis_required
async def test_the_per_app_budget_is_not_reported_as_the_user_total(budget_db):
    """⚠️ ``client IS NULL`` 필터가 없으면 앱 하나의 $3 한도가 전체 한도로 보고된다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    try:
        await r.delete(f"budget:config:user:{{{USER_ID}}}")
        body = await _call_usage_me(r, _factory(budget_db), "2026-06")
        assert Decimal(body["budget"]["max_usd"]) == Decimal("50.0000"), (
            f"per-app 예산이 전체로 보고됐다: {body['budget']['max_usd']}"
        )
    finally:
        await r.aclose()


@_pg_required
@_redis_required
async def test_redis_still_wins_for_the_live_spend(budget_db):
    """Redis 사용액은 실시간이고 DB 는 워커 커밋 뒤다 — Redis 가 있으면 그것을 쓴다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    period = "2026-06"
    try:
        await r.delete(f"budget:config:user:{{{USER_ID}}}")
        await r.set(f"budget:user:{{{USER_ID}}}:{period}", "20.0000")
        body = await _call_usage_me(r, _factory(budget_db), period)
        assert Decimal(body["budget"]["used_usd"]) == Decimal("20.0000"), (
            f"Redis 의 실시간 사용액이 무시됐다: {body['budget']['used_usd']}"
        )
        assert Decimal(body["budget"]["max_usd"]) == Decimal("50.0000")
    finally:
        await r.delete(f"budget:user:{{{USER_ID}}}:{period}")
        await r.aclose()


@_pg_required
@_redis_required
async def test_a_cached_config_is_used_without_touching_the_db(budget_db):
    """⚠️ 대조군. DB 폴백이 캐시를 덮어쓰지 않아야 한다 — 캐시가 실시간 소스다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    period = "2026-06"
    try:
        await r.set(
            f"budget:config:user:{{{USER_ID}}}",
            json.dumps({"limit_usd": "99.0000", "policy": "throttle"}),
        )
        body = await _call_usage_me(r, _factory(budget_db), period)
        assert Decimal(body["budget"]["max_usd"]) == Decimal("99.0000"), (
            f"캐시된 한도가 DB 값으로 덮였다: {body['budget']['max_usd']}"
        )
        assert body["budget"]["policy"] == "throttle"
    finally:
        await r.delete(f"budget:config:user:{{{USER_ID}}}")
        await r.aclose()


@_pg_required
@_redis_required
async def test_a_user_with_no_budget_at_all_still_gets_zeros_not_an_error(budget_db):
    """한도가 정말로 없는 사용자는 0 이 맞다 — 그 경로가 죽으면 사용량 조회 전체가 실패한다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)

    class _Other(_Auth):
        user_id = "69999999-9999-9999-9999-999999999999"

    from app.routers.usage import usage_me

    try:
        resp = await usage_me(
            _Req(
                {
                    "auth_context": _Other(),
                    "_redis": r,
                    "_session_factory": _factory(budget_db),
                }
            ),
            period="2026-06",
        )
        body = json.loads(bytes(resp.body))
        assert Decimal(body["budget"]["max_usd"]) == Decimal("0")
        assert body["budget"]["pct"] == 0.0
    finally:
        await r.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 참조되는 Lua 스크립트가 모두 존재하는지
# ─────────────────────────────────────────────────────────────────────────────


def test_every_referenced_lua_script_exists_on_disk():
    """⚠️ 이름이 틀리면 집행/환불이 **조용히** 멎는다.

    ``LuaScriptLoader.get`` 은 없는 이름에 KeyError 를 내고, 호출부의 except 가 그것을
    삼켜 로그만 남는다. 즉 스크립트 파일 하나를 지우거나 이름을 바꾸면 CPM/CPH 환불이
    아무 소리 없이 사라진다 — 이 결함을 고치는 과정에서 실제로 그렇게 됐다(테스트가
    Lua 를 로드하지 않아 환불이 no-op 이었고, 실패 메시지는 "환불 로직이 틀렸다" 처럼
    보였다).

    그래서 소스가 참조하는 이름 전부가 디스크에 있는지 본다.
    """
    import re

    src_root = Path(__file__).resolve().parents[2] / "src" / "app"
    script_dir = src_root / "redis_scripts"
    on_disk = {p.stem for p in script_dir.glob("*.lua")}
    assert on_disk, f"{script_dir} 에 .lua 가 없다 — 경로 확인"

    referenced: dict[str, str] = {}
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for name in re.findall(r'LuaScriptLoader\.get\(\s*["\']([A-Za-z0-9_]+)["\']', text):
            referenced.setdefault(name, str(py.relative_to(src_root)))
    assert referenced, "LuaScriptLoader.get 참조를 찾지 못했다 — 이 검사의 전제가 깨졌다"

    missing = {n: where for n, where in referenced.items() if n not in on_disk}
    assert not missing, f"참조되지만 파일이 없는 스크립트: {missing}"
    assert "cost_settle_scope" in referenced, (
        "비용 정산이 Lua 를 쓰지 않는다 — EXISTS 가드 없이 파이프라인으로 돌아갔다면 "
        "없는 키를 되살려 TTL 없는 키를 만든다"
    )
