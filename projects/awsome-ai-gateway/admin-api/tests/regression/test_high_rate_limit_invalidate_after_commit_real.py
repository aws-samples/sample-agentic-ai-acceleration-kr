# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""rate-limit 변경의 gateway-proxy 캐시 무효화가 **커밋 뒤에** 일어나는지 — 실 PostgreSQL + 실 Redis.

결함: 서비스가 upsert/비활성화 직후(커밋 전) ``rl:config:*`` 를 지웠고, 커밋은 CommittingRoute 가
응답 직전에 했다. 그 사이 도착한 게이트웨이 요청의 캐시 미스는 자기 커넥션으로 **커밋된(=옛)** 행을
읽어 300초 다시 캐시한다.

  * GLOBAL 한도를 바꾸면 옛 값이 콘솔이 새 값을 보여주는 동안 5분 더 집행됐다. GLOBAL 은 모델별이라
    (0046) 그 저장이 지우는 것은 **그 모델의** 키 하나다 — 다른 모델의 캐시와 행은 그대로 남아야 한다.
  * USER 오버라이드 제거도 같다 — 지운 개인 한도가 5분 더 집행됐다.

창을 결정적으로 만든다: 라우트가 커밋하는 **바로 그 순간 직전**에 게이트웨이 로더를 한 번 돌린다
(그 시점의 DB 는 아직 옛 상태다). 라우트 함수를 직접 부르고, 라우트가 커밋하지 않았으면 CommittingRoute
처럼 이어서 커밋한다. 끝난 뒤 캐시에 옛 한도가 남아 있으면 실패다.

게이트웨이 로더는 gateway-proxy ``rate_limit_config_loader`` 의 조회·캐시 규약을 그대로 옮긴 것이다
(같은 키·같은 WHERE·TTL 300). 그 모듈은 gateway-proxy 의 ``app`` 패키지를 import 해서 admin-api 와 한
프로세스에 같이 올릴 수 없다.

실행(둘 다 필요)::

    PROOF_DSN=postgresql+asyncpg://postgres:proof@127.0.0.1:55432/gwproof \\
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 \\
        pytest tests/regression/test_high_rate_limit_invalidate_after_commit_real.py

⚠️ ``PROOF_DSN`` 이 있는데 ``REDIS_PROOF_URL`` 이 없으면 skip 이 아니라 **실패**다 — CI 의 pg-proof 잡은
   둘 다 준다. 하나만 빠진 채 조용히 skip 되면 이 증명이 사라진 것을 아무도 모른다.
⚠️ ``PROOF_DSN`` 의 DB 는 건드리지 않는다. 자기 소유 ``<dbname>_rlcommit`` 을 만들어 쓰고 지운다.
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest

from tests.regression.test_high_console_contract_real_db import (
    SEED_ADMIN_ID,
    _apply_init_sql,
    _split_dsn,
)

PROOF_DSN = os.environ.get("PROOF_DSN")
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")
_OWNED_SUFFIX = "_rlcommit"

pytestmark = pytest.mark.skipif(
    not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL + 실 Redis 필요"
)

ALIAS_A = "claude-opus-4-7"  # GLOBAL 한도를 바꾸는 모델
ALIAS_B = "claude-sonnet-4-6"  # 자기 GLOBAL 한도를 가진 다른 모델 — A 의 저장에 영향받지 않아야 한다
U_OVR = uuid.UUID("dddd0000-0000-4000-a000-0000000000d1")  # 개인 오버라이드가 있는 사용자
TTL = 300  # gateway-proxy _CONFIG_CACHE_TTL_SEC


# ─────────────────────────────────────────────────────────────────────────────
# 픽스처
# ─────────────────────────────────────────────────────────────────────────────


async def _admin_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, _ = _split_dsn(PROOF_DSN)
    return create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="module")
async def owned_dsn():
    from sqlalchemy import text

    prefix, dbname = _split_dsn(PROOF_DSN)
    owned = f"{dbname}{_OWNED_SUFFIX}"
    admin = await _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{owned}"'))
    finally:
        await admin.dispose()
    dsn = f"{prefix}/{owned}"
    await _apply_init_sql(dsn)
    yield dsn
    admin = await _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest.fixture
async def redis_client():
    if not REDIS_PROOF_URL:
        pytest.fail("PROOF_DSN 은 있는데 REDIS_PROOF_URL 이 없다 — 이 증명은 실 Redis 가 필요하다")
    redis_asyncio = pytest.importorskip("redis.asyncio")
    client = redis_asyncio.from_url(REDIS_PROOF_URL, decode_responses=True)
    assert await client.ping() is True
    yield client
    async for key in client.scan_iter(match="rl:config:*"):
        await client.delete(key)
    async for key in client.scan_iter(match="ratelimit:config:*"):
        await client.delete(key)
    await client.aclose()


@pytest.fixture
async def engine(owned_dsn):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(owned_dsn)
    yield eng
    async with eng.begin() as conn:  # 다음 테스트를 위해 rate-limit 행을 비운다
        await conn.execute(text("DELETE FROM model.rate_limit_configs"))
    await eng.dispose()


def _admin():
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole

    # created_by 가 auth.users FK 라 시드 관리자여야 한다.
    return CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)


def _service(redis):
    from app.core.cache_invalidation import CacheInvalidationManager
    from app.services.rate_limit_service import RateLimitService

    return RateLimitService(cache_mgr=CacheInvalidationManager(redis))


def _request(svc):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(rate_limit_service=svc)), client=None, headers={}
    )


# ─────────────────────────────────────────────────────────────────────────────
# gateway-proxy rate_limit_config_loader 의 캐시 미스 경로(같은 키 · 같은 WHERE · 같은 TTL)
# ─────────────────────────────────────────────────────────────────────────────


async def _gateway_load(engine, redis, scope: str, scope_id: str | None, alias: str) -> dict:
    from sqlalchemy import text

    key = f"rl:config:{scope}:{scope_id if scope_id is not None else 'NULL'}:{alias}"
    raw = await redis.get(key)
    if raw is not None:
        return json.loads(raw)
    if scope == "GLOBAL":
        sql = (
            "SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs "
            "WHERE scope = 'GLOBAL' AND is_active AND scope_id IS NULL AND model_alias = :a"
        )
        params = {"a": alias}
    else:
        sql = (
            "SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs "
            "WHERE scope = CAST(:s AS model.rate_limit_scope) AND is_active AND scope_id = :sid "
            "AND (model_alias = :a OR model_alias IS NULL)"
        )
        params = {"s": scope, "sid": scope_id, "a": alias}
    async with engine.connect() as conn:  # 게이트웨이 pod = 별도 커넥션 → 커밋된 상태만 본다
        rows = (await conn.execute(text(sql), params)).all()
    chosen = next((r for r in rows if r.model_alias == alias), rows[0] if rows else None)
    limits = {
        "rpm": chosen.rpm_limit if chosen else None,
        "tpm": chosen.tpm_limit if chosen else None,
        "cpm": None,
        "cph": None,
    }
    await redis.set(key, json.dumps(limits), ex=TTL)
    return limits


async def _run_route_with_gateway_request_at_commit(session, call, on_commit):
    """``call()`` 을 실행하되, 세션이 커밋되는 순간 **직전**에 ``on_commit()`` 을 돌린다.
    라우트가 스스로 커밋하지 않았으면 CommittingRoute 처럼 이어서 커밋한다(그때도 직전에 돌린다)."""
    real_commit = session.commit
    window: list[dict] = []

    async def commit_with_gateway_request():
        window.append(await on_commit())
        await real_commit()

    session.commit = commit_with_gateway_request
    try:
        result = await call()
        if session.in_transaction():  # CommittingRoute: 핸들러 반환 뒤 · 응답 전 커밋
            await session.commit()
    finally:
        session.commit = real_commit
    assert len(window) == 1, "커밋이 정확히 한 번 일어나야 창을 재현한 것이다"
    return result, window[0]


# ─────────────────────────────────────────────────────────────────────────────
# 증명
# ─────────────────────────────────────────────────────────────────────────────


async def test_changed_global_limit_is_not_recached_and_other_models_keep_theirs(engine, redis_client):
    """GLOBAL 은 모델별이다(0046). A 를 바꾸는 저장의 커밋 창에 A 로 요청이 들어와도 옛 값이 남지 않고,
    B 의 GLOBAL 한도는 DB 에서도 캐시에서도 그대로다.

    대조군 두 개: (1) 커밋 **전에** 지우던 코드는 창의 요청이 다시 캐시한 옛 값(5)을 TTL 동안 남겼다.
    (2) 단일 슬롯 코드는 A 의 저장이 B 의 행을 해제했다(동의 없이는 409).
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.routers import rate_limits as routes
    from app.schemas.rate_limits import RateLimitSetRequest

    svc = _service(redis_client)

    # 1) 두 모델에 각자 GLOBAL 한도(커밋됨). 게이트웨이가 둘 다 캐시했다.
    for alias, rpm in ((ALIAS_A, 5), (ALIAS_B, 7)):
        async with AsyncSession(engine, expire_on_commit=False) as s:
            await routes.set_global_rate_limit(
                _request(svc), alias, RateLimitSetRequest(rpm=rpm), admin=_admin(), session=s
            )
            if s.in_transaction():
                await s.commit()
    assert (await _gateway_load(engine, redis_client, "GLOBAL", None, ALIAS_A))["rpm"] == 5
    assert (await _gateway_load(engine, redis_client, "GLOBAL", None, ALIAS_B))["rpm"] == 7

    # 2) 운영자가 A 를 9 로 바꾼다. 커밋 직전에 A 로 요청이 들어온다.
    await redis_client.delete(f"rl:config:GLOBAL:NULL:{ALIAS_A}")  # 창에서 반드시 DB 를 읽게 한다
    async with AsyncSession(engine, expire_on_commit=False) as s:
        _, in_window = await _run_route_with_gateway_request_at_commit(
            s,
            lambda: routes.set_global_rate_limit(
                _request(svc), ALIAS_A, RateLimitSetRequest(rpm=9), admin=_admin(), session=s
            ),
            lambda: _gateway_load(engine, redis_client, "GLOBAL", None, ALIAS_A),
        )
    assert in_window["rpm"] == 5, "전제: 커밋 전 창의 게이트웨이는 아직 옛 행을 본다"

    stale = await redis_client.get(f"rl:config:GLOBAL:NULL:{ALIAS_A}")
    assert stale is None, f"바꾼 GLOBAL 한도의 옛 값이 캐시에 남았다: {stale}"
    assert (await _gateway_load(engine, redis_client, "GLOBAL", None, ALIAS_A))["rpm"] == 9

    # B 는 건드리지 않았다 — 캐시가 남아 있고(지울 이유가 없다) DB 도 그 값이다.
    assert json.loads(await redis_client.get(f"rl:config:GLOBAL:NULL:{ALIAS_B}"))["rpm"] == 7
    await redis_client.delete(f"rl:config:GLOBAL:NULL:{ALIAS_B}")
    assert (await _gateway_load(engine, redis_client, "GLOBAL", None, ALIAS_B))["rpm"] == 7


async def test_removed_user_override_is_not_recached_from_the_commit_window(engine, redis_client):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.routers import rate_limits as routes
    from app.schemas.rate_limits import RateLimitSetRequest

    svc = _service(redis_client)
    uid = str(U_OVR)

    async with AsyncSession(engine, expire_on_commit=False) as s:
        await routes.set_user_rate_limit(
            _request(svc), uid, RateLimitSetRequest(rpm=3), admin=_admin(), session=s
        )
        if s.in_transaction():
            await s.commit()
    assert (await _gateway_load(engine, redis_client, "USER", uid, ALIAS_A))["rpm"] == 3

    await redis_client.delete(f"rl:config:USER:{uid}:{ALIAS_A}")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        out, in_window = await _run_route_with_gateway_request_at_commit(
            s,
            lambda: routes.delete_user_rate_limit(_request(svc), U_OVR, admin=_admin(), session=s),
            lambda: _gateway_load(engine, redis_client, "USER", uid, ALIAS_A),
        )
    assert out.deactivated == 1
    assert in_window["rpm"] == 3, "전제: 커밋 전 창의 게이트웨이는 아직 옛 행을 본다"

    stale = await redis_client.get(f"rl:config:USER:{uid}:{ALIAS_A}")
    assert stale is None, f"지운 개인 한도가 캐시에 남았다: {stale}"
    assert (await _gateway_load(engine, redis_client, "USER", uid, ALIAS_A))["rpm"] is None


async def test_a_new_limit_applies_even_if_a_request_lands_in_the_commit_window(engine, redis_client):
    """같은 결함의 반대 얼굴 — 새로 건 한도가 5분 늦게 적용됐다. 창의 게이트웨이 조회는 아직 행이 없어
    '한도 없음' 을 캐시하고, 커밋 전에 지웠으니 그 값이 TTL 동안 남았다. 커밋 뒤에 지우면 저장 후 첫
    조회가 새 한도를 읽는다."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.routers import rate_limits as routes
    from app.schemas.rate_limits import RateLimitSetRequest

    svc = _service(redis_client)
    team = str(uuid.uuid4())
    async with AsyncSession(engine, expire_on_commit=False) as s:
        _, in_window = await _run_route_with_gateway_request_at_commit(
            s,
            lambda: routes.set_team_rate_limit(
                _request(svc), team, RateLimitSetRequest(rpm=11), admin=_admin(), session=s
            ),
            lambda: _gateway_load(engine, redis_client, "TEAM", team, ALIAS_A),
        )
    assert in_window["rpm"] is None, "전제: 커밋 전 창에는 아직 팀 행이 없다"
    assert (await _gateway_load(engine, redis_client, "TEAM", team, ALIAS_A))["rpm"] == 11


async def test_failed_pattern_invalidation_is_retried_as_a_pattern(engine, redis_client):
    """패턴 무효화가 Redis 장애로 실패하면 ``pattern:<glob>`` 행이 실제 테이블에 남는다. 재시도
    (``POST /internal/cache/retry`` → ``retry_failed``)는 그 패턴을 다시 SCAN+DEL 해야 한다 — 예전엔
    리터럴 'pattern:…' 키를 DEL 하고 resolved 로 표시해, 옛 정책 캐시가 TTL 까지 남았다."""
    import redis.asyncio as redis_asyncio
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.cache_invalidation import CacheInvalidationManager

    uid = str(uuid.uuid4())
    stale = [f"rl:config:USER:{uid}:{alias}" for alias in (ALIAS_A, ALIAS_B)]
    for key in stale:
        await redis_client.set(key, json.dumps({"rpm": 3}), ex=TTL)
    bystander = f"rl:config:USER:{uuid.uuid4()}:{ALIAS_A}"
    await redis_client.set(bystander, json.dumps({"rpm": 9}), ex=TTL)

    # 1) Redis 장애 — 연결이 거부되는 주소. 실패가 실 PG 테이블에 기록된다.
    dead = redis_asyncio.from_url("redis://127.0.0.1:1/0")
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            await CacheInvalidationManager(dead).invalidate_pattern(f"rl:config:USER:{uid}:*", session=s)
            await s.commit()
    finally:
        await dead.aclose()
    pattern_key = f"pattern:rl:config:USER:{uid}:*"
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT resolved_at FROM audit.cache_invalidation_failures WHERE cache_key = :k"
        ), {"k": pattern_key})).one()
    assert row.resolved_at is None
    assert all([await redis_client.get(k) for k in stale]), "전제: 장애 동안 옛 캐시는 남아 있다"

    # 2) Redis 복구 후 재시도 — 패턴으로 다시 지운다.
    async with AsyncSession(engine, expire_on_commit=False) as s:
        resolved = await CacheInvalidationManager(redis_client).retry_failed(s)
        await s.commit()
    assert resolved == 1
    assert [await redis_client.get(k) for k in stale] == [None, None], "재시도가 패턴 키를 지우지 않았다"
    assert await redis_client.get(bystander) is not None  # 대조군 — 패턴 밖의 키는 그대로다
    async with engine.connect() as conn:
        resolved_at = (await conn.execute(text(
            "SELECT resolved_at FROM audit.cache_invalidation_failures WHERE cache_key = :k"
        ), {"k": pattern_key})).scalar_one()
    assert resolved_at is not None
