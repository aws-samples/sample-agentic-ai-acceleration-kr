# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""운영자가 설정한 예산 알림 임계값이 5분 뒤에도 살아 있는지.

무엇이 문제였나 — 기능이 DB 앞에서 끊겨 있었다
-----------------------------------------------
임계값 편집 기능은 **UI 부터 Lua 까지 전부 있었다.** 없는 것은 저장소뿐이었다:

  * ``admin-ui`` 의 SetBudgetDialog 가 임계값 편집기를 제공하고 ``api.ts`` 가 1~100 정수
    배열로 검증한다.
  * ``admin-api`` 가 그것을 받아 Redis 설정 JSON 에 ``"thresholds"`` 로 써 넣는다.
  * ``budget_deduct.lua`` 가 그 값으로 교차를 판정한다.

그런데 ``budget.budget_configs`` 에는 담을 컬럼이 없었고, admin-api 코드가 그 사실을 직접
적어 두고 있었다 — ``alert_thresholds=[80, 90, 100],  # DB에 컬럼 없음``.

결과는 **조용히 되돌아가는 설정**이다. 운영자가 50% 알림을 추가하면 admin-api 가 Redis 에
쓰고 알림이 실제로 동작한다. 그 키의 TTL 은 300초다. 만료되면 gateway-proxy 의 재수화가
DB 를 읽어 키를 다시 만드는데, 저장된 값이 없으므로 ``DEFAULT_THRESHOLDS`` 를 써 넣었다.

즉 운영자 설정은 **최대 5분** 살아 있었고, 그 뒤로는 오류도 경고도 없이 기본값으로
돌아갔다. 화면에는 여전히 50% 가 저장된 것처럼 보인다. 알림이 오지 않는 것은 "예산을 안
썼다" 와 구별되지 않으므로, 알아챌 방법이 없다.

⚠️ **실 PostgreSQL + 실 Redis** 로 확인한다. 이 결함의 본질은 "DB 에서 Redis 로 값이
   흐르는가" 이고, 두 저장소를 모두 진짜로 두지 않으면 그 흐름 자체가 존재하지 않는다.
   그리고 범위 제약은 PostgreSQL **도메인**이 배열 원소마다 적용하는 성질에 의존한다 —
   가짜에는 그 성질이 없다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services.budget_service import DEFAULT_THRESHOLDS, _row_thresholds

PROOF_DSN = os.environ.get("PROOF_DSN")
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

_pg_required = pytest.mark.skipif(
    not PROOF_DSN,
    reason=(
        "PROOF_DSN 미설정 — 실 PG 필요. DB→Redis 로 값이 흐르는지가 이 결함의 본질이고, "
        "도메인 제약은 실 PG 에서만 존재한다."
    ),
)
_redis_required = pytest.mark.skipif(
    not REDIS_PROOF_URL, reason="REDIS_PROOF_URL 미설정 — 재수화가 쓰는 대상이 Redis 다."
)

USER_ID = "71111111-1111-1111-1111-111111111111"
TEAM_ID = "72222222-2222-2222-2222-222222222222"
DEPT_ID = "73333333-3333-3333-3333-333333333333"
ORG_ID = "74444444-4444-4444-4444-444444444444"
_OWNED_SUFFIX = "_thrproof"


# ─────────────────────────────────────────────────────────────────────────────
# 1. 헬퍼의 판정 — 빈 배열과 미설정을 구별한다
# ─────────────────────────────────────────────────────────────────────────────


class _Row:
    def __init__(self, value):
        self.alert_thresholds = value


def test_an_empty_list_means_no_alerts_and_is_not_replaced_by_defaults():
    """⚠️ 이 구별이 이 결함의 핵심이다.

    ``or DEFAULT_THRESHOLDS`` 로 채우면 운영자가 **의도적으로 비운** 설정을 재수화가
    되살린다 — 끄려고 비운 알림이 5분 뒤 다시 켜진다.
    """
    assert _row_thresholds(_Row([])) == [], "빈 배열이 기본값으로 채워졌다"


def test_the_stored_values_are_returned_sorted_and_deduplicated():
    assert _row_thresholds(_Row([100, 50, 50, 80])) == [50, 80, 100]


def test_a_row_from_a_pre_0037_schema_falls_back_to_defaults():
    """컬럼이 없는 구 스키마에서만 기본값을 쓴다 — 그때는 속성 자체가 없다."""

    class _Old:
        pass

    assert _row_thresholds(_Old()) == list(DEFAULT_THRESHOLDS)
    # ⚠️ None 도 같은 취급이다. NOT NULL 컬럼이므로 정상 경로에서는 나올 수 없지만,
    #    나온다면 "미설정" 으로 읽는 것이 맞다(빈 목록으로 읽으면 알림이 사라진다).
    assert _row_thresholds(_Row(None)) == list(DEFAULT_THRESHOLDS)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 실 PG — 컬럼과 도메인
# ─────────────────────────────────────────────────────────────────────────────


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
async def dsn():
    asyncpg = pytest.importorskip("asyncpg")
    if not PROOF_DSN:
        pytest.skip("PROOF_DSN 미설정")
    url = await _make_db()
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute(
            "INSERT INTO auth.organizations (id, name) VALUES ($1,'thr-org') "
            "ON CONFLICT (id) DO NOTHING",
            ORG_ID,
        )
        await conn.execute(
            "INSERT INTO auth.departments (id, org_id, name) VALUES ($1,$2,'thr-dept') "
            "ON CONFLICT (id) DO NOTHING",
            DEPT_ID,
            ORG_ID,
        )
        await conn.execute(
            "INSERT INTO auth.teams (id, dept_id, name) VALUES ($1,$2,'thr-team') "
            "ON CONFLICT (id) DO NOTHING",
            TEAM_ID,
            DEPT_ID,
        )
        await conn.execute(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES ($1,$2,'thr@example.invalid','thr','ADMIN','thr-sub') "
            "ON CONFLICT (id) DO NOTHING",
            USER_ID,
            TEAM_ID,
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


async def _insert_config(dsn: str, thresholds, *, client=None, scope="USER", sid=None):
    """예산 행을 하나 만든다. ``thresholds=None`` 이면 컬럼 기본값을 쓴다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as c:
            await c.execute(
                text(
                    "DELETE FROM budget.budget_configs WHERE scope = :s AND scope_id = :i "
                    "AND client IS NOT DISTINCT FROM :c"
                ),
                {"s": scope, "i": sid or (USER_ID if scope == "USER" else TEAM_ID), "c": client},
            )
            cols = (
                "scope, scope_id, client, max_budget_usd, period_type, policy, allocated_by, "
                "effective_from, is_active"
            )
            vals = (
                ":s, :i, :c, 100.0000, 'MONTHLY', 'HARD_BLOCK', :ab, '2026-01-01', true"
            )
            params = {
                "s": scope,
                "i": sid or (USER_ID if scope == "USER" else TEAM_ID),
                "c": client,
                "ab": USER_ID,
            }
            if thresholds is not None:
                cols += ", alert_thresholds"
                vals += ", :t"
                params["t"] = thresholds
            await c.execute(
                text(f"INSERT INTO budget.budget_configs ({cols}) VALUES ({vals})"),
                params,
            )
    finally:
        await engine.dispose()


@_pg_required
async def test_the_column_exists_with_the_documented_default(dsn):
    """migration 0037 이 없으면 이 파일의 나머지가 전부 무의미하다 — 전제를 먼저 고정한다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as c:
            row = (
                await c.execute(
                    text(
                        "SELECT udt_name, is_nullable FROM information_schema.columns "
                        "WHERE table_schema='budget' AND table_name='budget_configs' "
                        "AND column_name='alert_thresholds'"
                    )
                )
            ).one_or_none()
        assert row is not None, "alert_thresholds 컬럼이 없다 — migration 0037 미적용"
        assert row.is_nullable == "NO", "NULL 을 허용하면 읽는 쪽마다 기본값 규칙이 필요해진다"
    finally:
        await engine.dispose()


@_pg_required
async def test_a_row_without_an_explicit_value_gets_the_standard_default(dsn):
    """기존 행이 지금 코드와 **같은** 값을 갖는다 — 마이그레이션 자체로 동작이 바뀌지 않는다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    await _insert_config(dsn, None)
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as c:
            got = (
                await c.execute(
                    text(
                        "SELECT alert_thresholds FROM budget.budget_configs "
                        "WHERE scope='USER' AND scope_id=:i AND client IS NULL"
                    ),
                    {"i": USER_ID},
                )
            ).scalar_one()
        assert list(got) == [80, 90, 100], got
    finally:
        await engine.dispose()


@_pg_required
@pytest.mark.parametrize("bad", [[0], [0, 80], [150], [101], [-5]])
async def test_out_of_range_values_are_rejected_by_the_database(dsn, bad):
    """⚠️ UI/API 를 거치지 않는 경로도 막아야 한다.

    ``0`` 은 특히 위험하다 — 사용량 0 에서 첫 요청이 오면 ``old_pct = 0`` 이고
    ``new_pct >= 0`` 이라 **모든 첫 요청마다** 알림이 나간다. ``150`` 은 반대로 절대
    발동하지 않고, 둘 다 조용하다.
    """
    import sqlalchemy.exc

    with pytest.raises(sqlalchemy.exc.DBAPIError):
        await _insert_config(dsn, bad)


@_pg_required
async def test_an_empty_array_is_accepted_as_a_real_setting(dsn):
    """빈 배열은 "알림 없음" 이고 유효하다 — 범위 제약이 그것까지 막으면 안 된다."""
    await _insert_config(dsn, [])
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as c:
            got = (
                await c.execute(
                    text(
                        "SELECT alert_thresholds FROM budget.budget_configs "
                        "WHERE scope='USER' AND scope_id=:i AND client IS NULL"
                    ),
                    {"i": USER_ID},
                )
            ).scalar_one()
        assert list(got) == []
    finally:
        await engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 실 PG + 실 Redis — 재수화가 운영자 값을 유지하는가
# ─────────────────────────────────────────────────────────────────────────────


def _factory(url: str):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    return async_sessionmaker(create_async_engine(url), expire_on_commit=False)


@_pg_required
@_redis_required
async def test_rehydrate_writes_the_operators_thresholds_not_the_defaults(dsn):
    """⚠️ 이 파일의 결론. 캐시가 만료된 뒤 재수화가 무엇을 쓰는지가 결함의 전부였다."""
    import redis.asyncio as aioredis

    from app.services.budget_service import BudgetService

    await _insert_config(dsn, [50, 75])
    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    config_key = f"budget:config:user:{{{USER_ID}}}"
    try:
        await r.delete(config_key)  # 만료된 상태를 만든다
        async with _factory(dsn)() as db:
            await BudgetService().ensure_config_cached(r, db, USER_ID)

        raw = await r.get(config_key)
        assert raw, "재수화가 키를 만들지 않았다 — 전제가 깨졌다"
        got = json.loads(raw)["thresholds"]
        assert got == [50, 75], (
            f"재수화가 임계값을 {got} 로 썼다 — 운영자 설정이 5분마다 기본값으로 "
            "되돌아가던 그 결함이다"
        )
    finally:
        await r.delete(config_key)
        await r.aclose()


@_pg_required
@_redis_required
async def test_rehydrate_preserves_an_empty_threshold_list(dsn):
    """⚠️ 운영자가 끈 알림이 되살아나면 안 된다 — ``or DEFAULT`` 의 함정."""
    import redis.asyncio as aioredis

    from app.services.budget_service import BudgetService

    await _insert_config(dsn, [])
    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    config_key = f"budget:config:user:{{{USER_ID}}}"
    try:
        await r.delete(config_key)
        async with _factory(dsn)() as db:
            await BudgetService().ensure_config_cached(r, db, USER_ID)
        got = json.loads(await r.get(config_key))["thresholds"]
        assert got == [], f"비워 둔 임계값이 {got} 로 되살아났다"
    finally:
        await r.delete(config_key)
        await r.aclose()


@_pg_required
@_redis_required
async def test_the_default_row_still_rehydrates_to_the_standard_thresholds(dsn):
    """⚠️ 대조군. 위 단정들이 "항상 DB 값" 으로 통과하는 것이 아님을 보인다.

    이것이 없으면 재수화가 임계값을 아예 쓰지 않아도(키에서 누락) 위 테스트가 통과할 수
    있다 — 그러면 Lua 가 자기 하드코딩 기본값으로 떨어지고 운영자 설정이 무시된다.
    """
    import redis.asyncio as aioredis

    from app.services.budget_service import BudgetService

    await _insert_config(dsn, None)  # 컬럼 기본값
    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    config_key = f"budget:config:user:{{{USER_ID}}}"
    try:
        await r.delete(config_key)
        async with _factory(dsn)() as db:
            await BudgetService().ensure_config_cached(r, db, USER_ID)
        payload = json.loads(await r.get(config_key))
        assert "thresholds" in payload, "재수화가 thresholds 키를 아예 쓰지 않았다"
        assert payload["thresholds"] == [80, 90, 100], payload["thresholds"]
    finally:
        await r.delete(config_key)
        await r.aclose()


@_pg_required
@_redis_required
async def test_the_team_rehydrate_uses_the_stored_value_too(dsn):
    """한 스코프만 고치면 그 스코프만 동작하고 재현 조건이 좁아진다."""
    import redis.asyncio as aioredis

    from app.services.budget_service import BudgetService

    await _insert_config(dsn, [60], scope="TEAM", sid=TEAM_ID)
    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    config_key = f"budget:config:team:{{{TEAM_ID}}}"
    try:
        await r.delete(config_key)
        async with _factory(dsn)() as db:
            await BudgetService()._hydrate_team_config_cache(r, db, TEAM_ID)
        got = json.loads(await r.get(config_key))["thresholds"]
        assert got == [60], f"팀 재수화가 {got} 를 썼다"
    finally:
        await r.delete(config_key)
        await r.aclose()


@_pg_required
@_redis_required
async def test_the_per_app_rehydrate_uses_the_stored_value_too(dsn):
    """per-app 예산도 같은 재수화 경로를 갖는다."""
    import redis.asyncio as aioredis

    from app.services.budget_service import BudgetService

    await _insert_config(dsn, [42], client="codex")
    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    config_key = f"budget:config:user:{{{USER_ID}}}:codex"
    try:
        await r.delete(config_key)
        async with _factory(dsn)() as db:
            await BudgetService()._hydrate_client_config_cache(r, db, USER_ID, "codex")
        got = json.loads(await r.get(config_key))["thresholds"]
        assert got == [42], f"per-app 재수화가 {got} 를 썼다"
    finally:
        await r.delete(config_key)
        await r.aclose()


@_pg_required
async def test_the_redis_degraded_path_also_reports_the_stored_thresholds(dsn):
    """⚠️ Redis 가 죽었을 때만 도는 경로다 — 그래서 결함이 오래 숨는다.

    이 경로에서 기본값을 쓰면 degrade 동안만 알림 기준이 달라진다. 그리고 처음 이 줄을
    고칠 때 스코프에 없는 이름(``config``)을 써서 **degrade 경로에서만 NameError** 가 되는
    잠재 결함을 만들었다 — import 는 통과하고, 이 테스트만이 잡는다.
    """
    from app.services.budget_service import BudgetService

    # 팀 예산만 두고(사용자 예산 없음) 팀 스코프로 판정되게 한다.
    await _insert_config(dsn, [55], scope="TEAM", sid=TEAM_ID)
    async with _factory(dsn)() as db:
        # 사용자 예산 행을 지워 팀 예산이 적용되는 상태로 만든다.
        from sqlalchemy import text

        await db.execute(
            text(
                "DELETE FROM budget.budget_configs WHERE scope='USER' AND scope_id=:i "
                "AND client IS NULL"
            ),
            {"i": USER_ID},
        )
        await db.commit()
        status = await BudgetService()._check_budget_db(
            db, USER_ID, TEAM_ID, "2026-06", None
        )
    assert list(status.thresholds) == [55], (
        f"degrade 경로가 임계값을 {status.thresholds} 로 보고했다 — DB 행의 값이어야 한다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. 구조 — 하드코딩이 되살아나지 않는지
# ─────────────────────────────────────────────────────────────────────────────


def test_no_rehydrate_path_writes_the_hardcoded_default_into_the_cache():
    """⚠️ 한 줄로 되돌아갈 수 있는 결함이고, 증상은 조용하다(5분 뒤 알림 기준이 바뀐다).

    AST 로 본다: 재수화가 만드는 config dict 의 ``"thresholds"`` 값이 상수/호출로
    ``DEFAULT_THRESHOLDS`` 를 가리키면 실패한다.
    """
    import ast

    src = (
        Path(__file__).resolve().parents[2] / "src" / "app" / "services" / "budget_service.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values, strict=False):
            if not (isinstance(k, ast.Constant) and k.value == "thresholds"):
                continue
            names = {n.id for n in ast.walk(v) if isinstance(n, ast.Name)}
            if "DEFAULT_THRESHOLDS" in names:
                offenders.append(node.lineno)
    assert not offenders, (
        f"L{offenders}: 재수화가 캐시에 하드코딩 기본값을 쓴다 — 운영자 설정이 캐시 TTL "
        "뒤에 되돌아간다"
    )
