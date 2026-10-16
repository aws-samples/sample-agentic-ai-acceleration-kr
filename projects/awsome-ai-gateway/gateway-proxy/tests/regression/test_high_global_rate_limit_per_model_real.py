# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GLOBAL rate limit 은 모델별이다(0046). 활성 GLOBAL 행이 여러 개인 DB 에서
게이트웨이가 모델마다 따로 집행하는지 실 PostgreSQL + 실 Redis 로 본다.

왜 필요한가
-----------
0046 이전에는 0024 의 유니크 인덱스가 활성 GLOBAL 행을 **전부 합쳐** 하나로
묶었다. "활성 GLOBAL 행이 여러 개" 는 게이트웨이가 본 적 없는 DB 상태였다.
롤링 배포에서는 마이그레이션 Job 과 admin-api 가 먼저 바뀌고 이전
gateway-proxy 파드는 그 뒤에 교체된다 — 그 사이 이전 게이트웨이가 이 상태를
읽는다. 잘못 읽는 방식은 셋이 가능하다:

  * 아무 행이나 고르기 — 다른 모델의 한도를 집행한다.
  * 다중 행 예외 — 그 스코프가 rl_config_db_query_failed 로 무제한이 된다.
  * 카운터 공유 — 한 모델의 트래픽이 다른 모델을 막는다.

이 파일이 고정하는 것
--------------------
  1. ``rate_limit_config_loader`` 는 GLOBAL 을
     ``scope_id IS NULL AND model_alias = <요청 alias>`` 로 읽는다. 행이 셋이어도
     각 alias 는 자기 행만 받고, 행이 없는 alias 는 무제한이다. 캐시 키도
     alias 별이다.
  2. ``enforce_rate_limits``(라우터 진입점) — 한 모델이 GLOBAL RPM 을 다 써서
     429 를 받아도 다른 모델은 자기 한도까지 통과한다. 카운터는
     ``{GLOBAL:*:<alias>}`` 로 갈린다. TPM 도 같다.
  3. 대조군: GLOBAL 카운터 키에서 alias 를 지우면(= 모델 간 공유) 2 가
     실패한다 — 이 테스트가 공유를 실제로 잡는다.

⚠️ 여기서 도는 gateway-proxy 코드는 0046 에서 바뀌지 않았다(GLOBAL 을 처음부터
   alias 로 읽었다). 즉 이 결과가 곧 **이전 릴리스의 게이트웨이**가 0046 DB 에서
   하는 일이다 — 배포 순서 제약이 없다는 근거다. Redis Cluster(CROSSSLOT)는
   tests/integration/test_rate_limit_cluster.py 가 본다.

⚠️ PROOF_DSN 은 db/init + alembic head(0046 포함)를 마친 DB 여야 한다 — 0045
   DB 에서는 시드(모델 셋의 활성 GLOBAL 행)부터 유니크 위반이다. 트랜잭션을
   롤백하므로 DB 에 남는 것이 없다. Redis 키는 실행마다 고유한 alias·사용자를
   쓰고 끝에 지운다.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

pytestmark = pytest.mark.skipif(
    not (PROOF_DSN and REDIS_PROOF_URL),
    reason="PROOF_DSN + REDIS_PROOF_URL 필요 — 실 PG·실 Redis 로만 증명된다.",
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
_SEED_ADMIN = "00000000-0000-4000-a000-000000000010"  # db/init 시드 관리자 (created_by FK)


@pytest.fixture
def run() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def aliases(run) -> dict[str, str]:
    # 실행마다 고유 — 공용 Redis 의 다른 키와 섞이지 않는다.
    return {k: f"zzproof-glob-{run}-{k}" for k in ("a", "b", "c", "d")}


@pytest.fixture
async def session(aliases):
    """모델 넷(a,b,c,d), 활성 GLOBAL 행 셋(a rpm=2, b rpm=3, c tpm=100).

    d 는 GLOBAL 행이 없다.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(PROOF_DSN)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as s:
            try:
                for alias in aliases.values():
                    await s.execute(
                        text(
                            "INSERT INTO model.model_aliases "
                            "(alias, provider, provider_model_id, api_format, status, created_by) "
                            "VALUES (:a, 'BEDROCK', 'anthropic.glob-proof', 'BEDROCK_NATIVE', "
                            "'ACTIVE', :by)"
                        ),
                        {"a": alias, "by": _SEED_ADMIN},
                    )
                for key, rpm, tpm in (("a", 2, None), ("b", 3, None), ("c", None, 100)):
                    await s.execute(
                        text(
                            "INSERT INTO model.rate_limit_configs "
                            "(scope, scope_id, model_alias, rpm_limit, tpm_limit,"
                            " is_active, created_by) "
                            "VALUES ('GLOBAL', NULL, :a, :rpm, :tpm, true, :by)"
                        ),
                        {"a": aliases[key], "rpm": rpm, "tpm": tpm, "by": _SEED_ADMIN},
                    )
                await s.flush()
                yield s
            finally:
                await s.rollback()  # 공용 proof DB 를 더럽히지 않는다
    finally:
        await engine.dispose()


@pytest.fixture
async def redis(aliases):
    import redis.asyncio as aioredis

    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader.load_all(_SRC / "redis_scripts")
    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    try:
        yield r
    finally:
        for alias in aliases.values():
            async for key in r.scan_iter(match=f"*{alias}*"):
                await r.delete(key)
        await r.aclose()


def _auth():
    from app.schemas.domain import AuthContext, AuthType, Role

    # USER/TEAM 한도가 없는 새 주체 — GLOBAL 만 집행된다.
    return AuthContext(
        user_id=str(uuid.uuid4()),
        team_id=str(uuid.uuid4()),
        dept_id=str(uuid.uuid4()),
        roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
    )


def _model(alias: str):
    from app.schemas.domain import (
        ApiFormat,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
    )

    return ModelConfigSchema(
        provider_model_id="anthropic.glob-proof",
        alias=alias,
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        pricing=ModelPricingSchema(input_per_1k=Decimal("0"), output_per_1k=Decimal("0")),
        status=ModelStatus.ACTIVE,
    )


async def _call(redis, session, alias: str, *, max_tokens: int = 10):
    """라우터처럼 ``enforce_rate_limits`` 를 한 번 부른다. 통과=None, 거부=429."""
    from app.services.rate_limit_enforcement import enforce_rate_limits

    @contextlib.asynccontextmanager
    async def _factory():
        yield session  # 시드가 든 트랜잭션을 그대로 쓴다(닫지 않는다)

    return await enforce_rate_limits(
        redis=redis,
        auth_context=_auth(),
        model_config=_model(alias),
        body={"max_tokens": max_tokens, "messages": [{"role": "user", "content": "hi"}]},
        state={"_session_factory": _factory},
        request_id=str(uuid.uuid4()),
    )


async def test_each_alias_reads_only_its_own_global_row(session, redis, aliases):
    from app.services.rate_limit_config_loader import load_all_scope_limits

    seen = {}
    for key, alias in aliases.items():
        limits = await load_all_scope_limits(
            redis=redis, db=session, user_id=str(uuid.uuid4()), team_id=None, model_alias=alias
        )
        seen[key] = (limits.global_.rpm, limits.global_.tpm)
        cached = json.loads(await redis.get(f"rl:config:GLOBAL:NULL:{alias}"))
        assert (cached["rpm"], cached["tpm"]) == seen[key], "캐시도 alias 별이다"
    assert seen == {"a": (2, None), "b": (3, None), "c": (None, 100), "d": (None, None)}


async def _allowed_until_429(redis, session, alias: str, *, attempts: int) -> list[bool]:
    out = []
    for _ in range(attempts):
        resp = await _call(redis, session, alias)
        if resp is not None:
            assert resp.status_code == 429
            headers = (resp.headers["X-RateLimit-Scope"], resp.headers["X-RateLimit-Type"])
            assert headers == ("GLOBAL", "rpm")
        out.append(resp is None)
    return out


async def test_one_models_global_rpm_does_not_throttle_another(session, redis, aliases):
    a, b = aliases["a"], aliases["b"]
    assert await _allowed_until_429(redis, session, a, attempts=3) == [True, True, False]
    # a 는 한도를 다 썼다 — b 는 자기 한도(3)까지 그대로 통과한다.
    assert await _allowed_until_429(redis, session, b, attempts=4) == [True, True, True, False]
    assert await _allowed_until_429(redis, session, a, attempts=1) == [False]
    counts = [await redis.zcard(f"{{GLOBAL:*:{x}}}:rpm") for x in (a, b)]
    assert counts == [2, 3]
    # 한도가 없는 모델은 GLOBAL 카운터 자체가 없다(그리고 통과한다).
    assert await _call(redis, session, aliases["d"]) is None
    assert await redis.exists(f"{{GLOBAL:*:{aliases['d']}}}:rpm") == 0


async def test_one_models_global_tpm_does_not_throttle_another(session, redis, aliases):
    c, a = aliases["c"], aliases["a"]
    assert await _call(redis, session, c, max_tokens=60) is None  # 예약 ~70 ≤ 100
    rejected = await _call(redis, session, c, max_tokens=60)  # 누적 ~140 > 100
    assert rejected is not None and rejected.status_code == 429
    headers = (rejected.headers["X-RateLimit-Scope"], rejected.headers["X-RateLimit-Type"])
    assert headers == ("GLOBAL", "tpm")
    # a 에는 GLOBAL TPM 이 없다 — c 의 토큰 예약과 무관하게 통과한다.
    assert await _call(redis, session, a, max_tokens=60) is None
    assert await redis.exists(f"{{GLOBAL:*:{a}}}:tpm:cur") == 0


async def test_control_a_shared_global_counter_would_throttle_the_other_model(
    session, redis, aliases, monkeypatch
):
    """대조군 — GLOBAL 카운터 키에서 alias 를 지우면 a 의 두 요청이 b 의 한도(3)를
    갉아먹어 b 는 한 번만 통과한다. 위 테스트의 [True, True, True, False] 가 공유
    카운터를 실제로 잡는다는 증거다."""
    from app.services import rate_limit_service as rls
    from app.services.rate_limit_scope import RateLimitScope, build_rl_key

    shared = f"shared-{aliases['a']}"

    def _alias_blind(scope, scope_id, model_alias, metric):
        if scope is RateLimitScope.GLOBAL:
            model_alias = shared
        return build_rl_key(scope, scope_id, model_alias, metric)

    monkeypatch.setattr(rls, "build_rl_key", _alias_blind)
    try:
        assert await _allowed_until_429(redis, session, aliases["a"], attempts=2) == [True, True]
        b_seq = await _allowed_until_429(redis, session, aliases["b"], attempts=3)
        assert b_seq == [True, False, False]
    finally:
        await redis.delete(f"{{GLOBAL:*:{shared}}}:rpm")
