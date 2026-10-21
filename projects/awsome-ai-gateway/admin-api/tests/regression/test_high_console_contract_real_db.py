# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""백엔드↔콘솔 계약 수정분을 **실 PostgreSQL** 로 증명한다 (PROOF_DSN).

여기 모인 것은 전부 mock 으로는 증명이 원리적으로 불가능한 것들이다 — 검증 대상이
"WHERE 경계가 KST 로 정확히 잘리는가", "조인/GROUP BY 가 실제 스키마 위에서 도는가",
"NUMERIC 컬럼이 무엇을 거부하는가" 이기 때문이다. 단위 테스트의 SQL 문자열 단정은
경계 리터럴이 **있다**는 것만 보고, 그 경계로 **집계한 결과가 맞는지**는 보지 못한다.

  1. /admin/analytics 의 일자 범위(start_date/end_date) — 요약·모델·팀·사용자·추이가
     전부 같은 KST 범위로 잘리고, 범위 밖(월의 나머지)·실패 호출은 섞이지 않는다.
  2. /admin/rate-limits 트리·실시간 사용량·오버라이드 제거 — 팀 미배정 사용자 조회
     (team_id IS NULL), 활성 alias 목록, 활성 GLOBAL 행, 상속 표식, ``measured`` 의 근거인
     "자기 스코프 활성 rpm 행", DELETE 후 재설정이 유니크 인덱스(0024)와 공존하는지, 그리고
     GLOBAL 이 **모델별**이라는 것(0046) — 한 모델을 저장하거나 비워도 다른 모델의 GLOBAL 한도는
     그대로 활성이고, 트리가 둘을 함께 보여준다.
  3. ``SetBudgetRequest.max_budget_usd`` 의 상한(max_digits=12, decimal_places=4)이
     ``budget_configs.max_budget_usd NUMERIC(12,4)`` 와 **정확히 같은 경계**인지 — 스키마가
     통과시키는 최댓값은 컬럼이 받고, 스키마가 거부하는 최솟값은 컬럼도 거부한다(그 값이
     예전에는 스키마를 통과해 asyncpg overflow → 500 이 됐다).
  4. 채팅 assistant 메시지 INSERT 가 ``duration_ms`` 를 실제 스키마(0005)에 쓰고, 그 값이 done
     프레임의 ``durationMs`` 와 같다(⚠️ chat_agent 스키마는 migration 소유라 init SQL 로 만든
     owned DB 에는 없다 — 이 테스트만 alembic head 의 PROOF_DSN DB 를 쓰고 만든 행을 지운다).
  5. /admin/monitoring/models 의 status 가 등록부 LEFT JOIN 으로 ACTIVE / INACTIVE /
     UNREGISTERED 를 실제로 구분하고, 등록부에 없는 alias 의 트래픽이 사라지지 않는다.
  6. /admin/dashboard/top-teams 가 동명 팀을 서로 다른 ``team_id`` 의 두 행으로 낸다(콘솔이 이름으로
     조인하던 동안 두 팀의 소진율이 뒤바뀌었다 — 이 스키마에는 팀 이름 유일성 제약이 아예 없다).
  7. 자동 하향 PUT 의 ``enabled: false`` 가 budget.downgrade_policies 의 활성 행을 실제로 없앤다
     (예전: 규칙을 전부 is_active=true 로 다시 써서 '끄기' 저장 후에도 집행이 계속됐다).
  8. analytics scope 는 닫힌 집합이다 — TEAM_LEADER 가 'xyz'·'ALL'·' all' 로 물으면 데이터를 읽기 전에
     400 이고, 'all' 은 자기 팀으로만 좁혀진다(예전: 앞의 셋이 전사 범위로 떨어져 다른 팀 사용자
     이메일까지 화면·CSV 로 나갔다).
  9. 팀 예산 배정(PUT /admin/budgets/team/{id}/allocate)은 그 팀의 멤버에게만 쓴다 — 리더가 자기 팀
     URL 로 다른 팀 사용자의 개인 예산을 덮어쓰지 못한다(예전: user_id 의 소속을 보지 않았다). 팀 없는
     리더는 팀 없는 사용자의 예산도 다루지 못한다(예전: None != None 이 False 라 통과).
  10. /admin/my/budget 의 팀 예산 상태 — 미배정 / 예산 없음 / 있음, 그리고 예산이 있어도 게이트웨이가
     거부하는 **소진** 상태(이번 KST 월의 budget_usages TEAM 행으로, 지난달 행은 섞이지 않는다).
  11. TEAM_LEADER 의 예산 읽기 범위 — 다른 팀의 배정·자동 하향은 403, 예산 요약은 자기 팀과 그 멤버로
     걸러진다(전사·팀 없는 사용자 제외). 팀 없는 리더는 아무 팀도 못 읽는다. 실제 budgets 라우터로 본다.

실행::

    docker run -d --name pg-proof -p 55432:5432 -e POSTGRES_PASSWORD=proof \\
        -e POSTGRES_DB=gwproof pgvector/pgvector:pg16
    PROOF_DSN=postgresql+asyncpg://postgres:proof@127.0.0.1:55432/gwproof \\
        pytest tests/regression/test_high_console_contract_real_db.py

⚠️ ``PROOF_DSN`` 의 데이터베이스는 **건드리지 않는다.** 자기 소유 ``<dbname>_console``
   을 만들고 ``db/init/*.sql`` 을 적용해 쓰고 지운다(test_high_analytics_seed_reconciliation
   과 같은 규약 — 공용 DB 에 행을 섞으면 다른 증명 테스트의 숫자가 움직인다).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_console"
_INIT_DIR = Path(__file__).resolve().parents[3] / "db" / "init"

pytestmark = pytest.mark.skipif(
    not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL 필요(mock/SQLite 로는 검증 불가)"
)

KST = timezone(timedelta(hours=9))

# init SQL(03_seed_data) 이 만드는 고정 행.
SEED_ADMIN_ID = uuid.UUID("00000000-0000-4000-a000-000000000010")
SEED_TEAM_ID = uuid.UUID("00000000-0000-4000-a000-000000000003")


# ─────────────────────────────────────────────────────────────────────────────
# 자체 소유 DB 부트스트랩
# ─────────────────────────────────────────────────────────────────────────────


def _split_dsn(dsn: str) -> tuple[str, str]:
    prefix, _, dbname = dsn.rpartition("/")
    assert prefix and dbname, f"DSN 형식이 이상하다: {dsn}"
    return prefix, dbname


def _asyncpg_dsn(sa_dsn: str) -> str:
    return sa_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _recreate_owned_db() -> str:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, dbname = _split_dsn(PROOF_DSN)
    owned = f"{dbname}{_OWNED_SUFFIX}"
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{owned}"'))
    finally:
        await admin.dispose()
    return f"{prefix}/{owned}"


async def _drop_owned_db() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, dbname = _split_dsn(PROOF_DSN)
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(
                text(f'DROP DATABASE IF EXISTS "{dbname}{_OWNED_SUFFIX}" WITH (FORCE)')
            )
    finally:
        await admin.dispose()


async def _apply_init_sql(dsn: str) -> None:
    """실 ``db/init/*.sql`` 을 그대로 적용한다 — DDL 을 테스트에 복제하지 않는다.

    asyncpg 를 직접 쓰는 이유: init SQL 은 ``DO $$ … $$`` 와 멀티 스테이트먼트를 담고
    있어 SQLAlchemy 의 prepared statement 경로가 거부한다.
    """
    asyncpg = pytest.importorskip("asyncpg")
    files = sorted(_INIT_DIR.glob("*.sql"))
    assert len(files) >= 4, f"init SQL 을 {len(files)}개만 찾았다 — 경로 확인: {_INIT_DIR}"
    conn = await asyncpg.connect(_asyncpg_dsn(dsn))
    try:
        for f in files:
            try:
                await conn.execute(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001 - 어떤 파일이 왜 실패했는지 보여야 한다
                print(f"  [init] {f.name} 실패(계속): {type(e).__name__}: {e}")
        # 대조군 — 이 파일이 요구하는 테이블이 실제로 생겼는가(없이 통과하면 공허하다).
        for schema, table in [
            ("auth", "users"),
            ("auth", "teams"),
            ("usage", "usage_logs"),
            ("budget", "budget_usages"),
            ("model", "model_aliases"),
            ("model", "rate_limit_configs"),
        ]:
            n = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = $1 AND table_name = $2",
                schema,
                table,
            )
            assert n == 1, f"{schema}.{table} 이 없다 — init SQL 적용 실패"
    finally:
        await conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 시드 — 1. analytics 일자 범위
# ─────────────────────────────────────────────────────────────────────────────

PERIOD = "2026-09"
TEAM_B_ID = uuid.UUID("bbbb0000-0000-4000-a000-0000000000b0")
U_A = uuid.UUID("aaaa0000-0000-4000-a000-0000000000a1")  # team = seed team
U_B = uuid.UUID("aaaa0000-0000-4000-a000-0000000000b1")  # team = TEAM_B
U_SEED = uuid.UUID("aaaa0000-0000-4000-a000-0000000000c1")  # seed team, 월 말 사용 + 이관분
ALIAS = "claude-sonnet-4-6"


def _kst(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=KST)


async def _seed_analytics(conn) -> None:
    dept_id = await conn.fetchval("SELECT dept_id FROM auth.teams WHERE id = $1", SEED_TEAM_ID)
    assert dept_id is not None, "시드 팀이 없다 — init SQL 의 03_seed_data 확인"
    await conn.execute(
        "INSERT INTO auth.teams (id, dept_id, name) VALUES ($1, $2, 'Range Team B')",
        TEAM_B_ID,
        dept_id,
    )
    for uid, team, email in [
        (U_A, SEED_TEAM_ID, "range-a@example.com"),
        (U_B, TEAM_B_ID, "range-b@example.com"),
        (U_SEED, SEED_TEAM_ID, "range-seed@example.com"),
    ]:
        await conn.execute(
            "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
            "VALUES ($1, $2, $3, $4, 'DEVELOPER', $5)",
            uid,
            team,
            email,
            email.split("@")[0],
            f"sub-{uid}",
        )
    provider = await conn.fetchval(
        "SELECT provider::text FROM model.model_aliases WHERE alias = $1", ALIAS
    )
    assert provider, f"시드 alias {ALIAS} 가 없다"

    async def log(user, team, at: datetime, cost: str, status: str = "SUCCESS"):
        await conn.execute(
            "INSERT INTO usage.usage_logs "
            "(id, request_id, user_id, team_id, dept_id, model_alias, provider, status, "
            " requested_at, completed_at, cost_usd, input_tokens, output_tokens, "
            " cache_read_tokens, cache_creation_tokens, latency_ms) "
            "VALUES (gen_random_uuid(), gen_random_uuid()::text, $1, $2, $3, $4, $5, "
            "        $6::usage.usage_status, $7, $7, $8, 10, 20, 0, 0, 100)",
            user, team, dept_id, ALIAS, provider, status, at, Decimal(cost),
        )

    # 경계가 핵심이다 — 범위 [09-01, 09-05] 는 KST 09-01 00:00 ~ 09-06 00:00(반개구간).
    await log(U_A, SEED_TEAM_ID, _kst(9, 1, 0, 30), "1.00")  # = UTC 08-31 15:30 → 범위 안
    await log(U_A, SEED_TEAM_ID, _kst(9, 5, 23, 59), "2.00")  # 범위 마지막 순간 → 안
    await log(U_A, SEED_TEAM_ID, _kst(9, 6, 0, 0), "4.00")  # 다음 날 0시 → 밖(월 안)
    await log(U_B, TEAM_B_ID, _kst(9, 3, 12), "8.00")  # 범위 안, 다른 팀
    await log(U_B, TEAM_B_ID, _kst(9, 3, 13), "16.00", status="ERROR")  # 실패 → 어디에도 없음
    await log(U_B, TEAM_B_ID, _kst(8, 31, 23, 59), "32.00")  # 전월 마지막 순간 → 9월 밖
    await log(U_SEED, SEED_TEAM_ID, _kst(9, 20, 12), "64.00")  # 월 안, 범위 밖
    # 이관분(seed) = 164 − 64(월 전-status 실사용) = 100
    await conn.execute(
        "INSERT INTO budget.budget_usages "
        "(id, scope, scope_id, period, client, used_usd, limit_usd, last_updated) "
        "VALUES (gen_random_uuid(), 'USER'::budget.budget_scope, $1, $2, NULL, 164.00, 1000, now())",
        U_SEED,
        PERIOD,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 시드 — 2. rate-limit 트리
# ─────────────────────────────────────────────────────────────────────────────

U_TEAMLESS = uuid.UUID("aaaa0000-0000-4000-a000-0000000000d1")
RETIRED_ALIAS = "llama-3-70b"  # 비활성으로 내리고 GLOBAL 한도를 남긴다
BRACKET_ALIAS = "claude-sonnet-4-6[1m]"


async def _seed_rate_limits(conn) -> None:
    await conn.execute(
        "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
        "VALUES ($1, NULL, 'rl-teamless@example.com', 'rl-teamless', 'DEVELOPER', $2)",
        U_TEAMLESS,
        f"sub-{U_TEAMLESS}",
    )

    async def rl(scope, scope_id, *, alias=None, rpm=None, tpm=None):
        await conn.execute(
            "INSERT INTO model.rate_limit_configs "
            "(id, scope, scope_id, model_alias, rpm_limit, tpm_limit, is_active, created_by) "
            "VALUES (gen_random_uuid(), $1::model.rate_limit_scope, $2, $3, $4, $5, true, $6)",
            scope, scope_id, alias, rpm, tpm, SEED_ADMIN_ID,
        )

    await rl("TEAM", SEED_TEAM_ID, rpm=100, tpm=9000)
    await rl("USER", U_A, rpm=60)
    await conn.execute(
        "UPDATE model.model_aliases SET status = 'INACTIVE' WHERE alias = $1", RETIRED_ALIAS
    )
    await rl("GLOBAL", None, alias=RETIRED_ALIAS, tpm=5000)  # TPM 만 — rpm 카운터 없음


# ─────────────────────────────────────────────────────────────────────────────
# 픽스처
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def owned_dsn():
    asyncpg = pytest.importorskip("asyncpg")
    dsn = await _recreate_owned_db()
    await _apply_init_sql(dsn)
    conn = await asyncpg.connect(_asyncpg_dsn(dsn))
    try:
        await _seed_analytics(conn)
        await _seed_rate_limits(conn)
    finally:
        await conn.close()
    yield dsn
    await _drop_owned_db()


@pytest.fixture
async def session(owned_dsn):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(owned_dsn)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _admin():
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole

    return CurrentUser(user_id=uuid.uuid4(), email="admin@example.com", role=UserRole.ADMIN, team_id=None)


async def _analytics(session, *, group_by="model", start=None, end=None, period=PERIOD):
    from app.services.analytics_service import AnalyticsService

    return await AnalyticsService().get_analytics(
        session,
        period=period,
        group_by=group_by,
        scope="all",
        start_date=start,
        end_date=end,
        actor=_admin(),
    )


def _ours(items, attr="email"):
    """시드 데이터에서 온 행만 — init SQL 이 다른 사용 로그를 넣지는 않지만, 넣더라도 섞이지 않게."""
    return {getattr(i, attr): i for i in items if (getattr(i, attr) or "").startswith("range-")}


# ─────────────────────────────────────────────────────────────────────────────
# 1. analytics 일자 범위
# ─────────────────────────────────────────────────────────────────────────────


async def test_month_request_is_the_control(session):
    """대조군 — 범위 없이 월 전체: SUCCESS 만, 9월 KST 만(전월 32·실패 16 제외)."""
    resp = await _analytics(session)
    assert resp.start_date is None and resp.end_date is None
    assert resp.cost_summary.total_cost_usd == Decimal("79.00")  # 1+2+4+8+64
    assert resp.cost_summary.total_requests == 5


async def test_range_narrows_every_summary_aggregate(session):
    """[09-01, 09-05] = 1 + 2 + 8 = 11. 범위가 무시되면 위 대조군의 79 가 나온다."""
    resp = await _analytics(session, start="2026-09-01", end="2026-09-05")
    assert (resp.start_date, resp.end_date) == ("2026-09-01", "2026-09-05")
    s = resp.cost_summary
    assert s.total_cost_usd == Decimal("11.00"), "범위가 요약 카드에 반영되지 않았다"
    assert s.total_requests == 3
    assert s.active_users == 2
    assert s.total_tokens == 3 * 30
    assert {m.model: (m.cost_usd, m.requests) for m in resp.by_model} == {
        ALIAS: (Decimal("11.00"), 3)
    }


async def test_range_boundaries_are_kst_half_open(session):
    """KST 자정 경계 양쪽 — 09-01 00:30 KST(=UTC 전날 15:30)는 안, 09-06 00:00 KST 는 밖."""
    first_day = await _analytics(session, start="2026-09-01", end="2026-09-01")
    assert first_day.cost_summary.total_cost_usd == Decimal("1.00")
    after = await _analytics(session, start="2026-09-06", end="2026-09-06")
    assert after.cost_summary.total_cost_usd == Decimal("4.00")


async def test_cross_month_range_reaches_into_the_previous_month(session):
    """[08-31, 09-01] — 전월 마지막 순간(32)과 9월 첫 건(1). 월 필터로는 만들 수 없는 질의다.
    period 는 start_date 의 달이어야 한다(admin-ui 가 그렇게 보낸다)."""
    resp = await _analytics(session, start="2026-08-31", end="2026-09-01", period="2026-08")
    assert resp.cost_summary.total_cost_usd == Decimal("33.00")


async def test_range_narrows_team_breakdown_and_trends(session):
    resp = await _analytics(session, group_by="team", start="2026-09-01", end="2026-09-05")
    by_team = {t.team_id: t.cost_usd for t in resp.by_team}
    assert by_team == {str(SEED_TEAM_ID): Decimal("3.00"), str(TEAM_B_ID): Decimal("8.00")}
    assert [(t.date, t.cost_usd) for t in resp.trends] == [
        ("2026-09-01", Decimal("1.00")),
        ("2026-09-03", Decimal("8.00")),
        ("2026-09-05", Decimal("2.00")),
    ], "추이 차트가 범위 밖 날짜(09-06, 09-20)를 그린다"


async def test_month_to_date_range_keeps_the_seed(session):
    """월 초 시작 범위는 `/usage/by-user` 와 같은 규약으로 월 seed 를 더한다(U_SEED: 0 + 100)."""
    resp = await _analytics(session, group_by="user", start="2026-09-01", end="2026-09-05")
    ours = _ours(resp.by_user)
    assert ours["range-a@example.com"].cost_usd == Decimal("3.00")
    assert ours["range-b@example.com"].cost_usd == Decimal("8.00")
    assert ours["range-seed@example.com"].cost_usd == Decimal("100.00")


async def test_mid_month_range_does_not_attribute_the_seed(session):
    """음성 대조 — 09-02 시작 범위에 월 seed 를 더하면 이관 이전 금액이 그 며칠에 귀속된다."""
    resp = await _analytics(session, group_by="user", start="2026-09-02", end="2026-09-05")
    ours = _ours(resp.by_user)
    assert "range-seed@example.com" not in ours, "중간 범위에 seed 가 섞였다"
    assert ours["range-a@example.com"].cost_usd == Decimal("2.00")


async def test_month_mode_by_user_is_unchanged(session):
    """대조군 — 월 질의의 seed 반영(64 + 100)은 그대로다."""
    resp = await _analytics(session, group_by="user")
    assert _ours(resp.by_user)["range-seed@example.com"].cost_usd == Decimal("164.00")


async def test_team_export_csv_follows_the_range_and_axis(session):
    import csv
    import io

    from app.services.analytics_service import AnalyticsService

    content, ctype = await AnalyticsService().export_analytics(
        session,
        format="csv",
        period=PERIOD,
        group_by="team",
        start_date="2026-09-01",
        end_date="2026-09-05",
        actor=_admin(),
    )
    assert ctype == "text/csv"
    rows = list(csv.reader(io.StringIO(content)))
    assert rows[0] == ["period", "team", "team_id", "cost_usd", "active_users"]
    got = {r[2]: (r[0], Decimal(r[3])) for r in rows[1:]}
    assert got[str(TEAM_B_ID)] == ("2026-09-01~2026-09-05", Decimal("8"))
    assert got[str(SEED_TEAM_ID)] == ("2026-09-01~2026-09-05", Decimal("3"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. rate-limit 트리 / 실시간 사용량 / 오버라이드 제거
# ─────────────────────────────────────────────────────────────────────────────


def _rl_service():
    """Redis 는 대역(빈 SCAN) — 여기서 증명할 것은 DB 쪽이다. 실 Redis SCAN 은
    test_high_rate_limit_live_usage_real_redis.py 가 따로 증명한다."""
    from unittest.mock import AsyncMock, MagicMock

    from app.core.cache_invalidation import CacheInvalidationManager
    from app.services.rate_limit_service import RateLimitService

    async def _empty(*_a, **_kw):
        if False:  # pragma: no cover
            yield None

    redis = AsyncMock()
    redis.scan_iter = MagicMock(side_effect=lambda *a, **kw: _empty())
    return RateLimitService(cache_mgr=CacheInvalidationManager(redis))


async def _tree(session):
    return await _rl_service().get_rate_limit_tree(session)


def _find(nodes, node_id: str):
    for n in nodes:
        if n.id == node_id:
            return n
        for c in n.children:
            if c.id == node_id:
                return c
    return None


async def _active_rows(session, scope: str, scope_id=None):
    from sqlalchemy import text

    sql = (
        "SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs "
        "WHERE scope = CAST(:scope AS model.rate_limit_scope) AND is_active "
        + ("AND scope_id = :sid" if scope_id is not None else "AND scope_id IS NULL")
    )
    params = {"scope": scope, **({"sid": scope_id} if scope_id is not None else {})}
    return (await session.execute(text(sql), params)).all()


async def test_tree_lists_every_active_alias_as_a_global_root(session):
    from sqlalchemy import text

    active = {
        r[0]
        for r in (
            await session.execute(text("SELECT alias FROM model.model_aliases WHERE status = 'ACTIVE'"))
        ).all()
    }
    assert BRACKET_ALIAS in active, "시드 전제가 깨졌다"
    nodes = await _tree(session)
    global_ids = {n.id for n in nodes if n.scope == "GLOBAL"}
    assert active <= global_ids, f"트리에 없는 활성 alias: {sorted(active - global_ids)}"
    assert _find(nodes, BRACKET_ALIAS).config is None


async def test_inactive_alias_with_a_live_global_cap_is_still_listed(session):
    nodes = await _tree(session)
    retired = _find(nodes, RETIRED_ALIAS)
    assert retired is not None and retired.scope == "GLOBAL"
    assert retired.config is not None
    assert (retired.config.rpm, retired.config.tpm, retired.config.target_id) == (
        None,
        5000,
        RETIRED_ALIAS,
    )


async def test_teamless_user_is_a_root(session):
    nodes = await _tree(session)
    roots = {n.id: n for n in nodes if n.scope == "USER"}
    assert str(U_TEAMLESS) in roots, "team_id IS NULL 사용자가 트리에서 빠졌다"
    assert roots[str(U_TEAMLESS)].inherited_from is None
    # 팀 멤버는 루트로 중복 등장하지 않는다.
    assert str(U_A) not in roots


async def test_inherited_member_carries_the_team_marker(session):
    nodes = await _tree(session)
    seed_member = _find(nodes, str(U_SEED))  # 시드 팀 소속, 자기 행 없음
    assert seed_member.inherited_from == str(SEED_TEAM_ID)
    assert seed_member.config.scope == "TEAM" and seed_member.config.rpm == 100
    own = _find(nodes, str(U_A))
    assert own.inherited_from is None and own.config.scope == "USER" and own.config.rpm == 60


async def test_measured_reads_only_the_subjects_own_rpm_row(session):
    svc = _rl_service()
    own = await svc.get_live_usage("USER", str(U_A), session=session)
    assert (own["measured"], own["limit_rpm"]) == (True, 60)
    # 상속 중인 사용자에게는 USER 카운터가 생기지 않는다 — 팀 한도를 끌어오면 거짓 '측정'.
    inheriting = await svc.get_live_usage("USER", str(U_SEED), session=session)
    assert (inheriting["measured"], inheriting["limit_rpm"]) == (False, None)
    team = await svc.get_live_usage("TEAM", str(SEED_TEAM_ID), session=session)
    assert (team["measured"], team["limit_rpm"]) == (True, 100)
    # TPM 만 걸린 GLOBAL 은 rpm 카운터가 없다 → 측정 불가가 정답이다.
    glob = await svc.get_live_usage("GLOBAL", RETIRED_ALIAS, session=session)
    assert (glob["measured"], glob["limit_rpm"]) == (False, None)


async def test_delete_override_restores_inheritance_and_reset_still_works(session):
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.schemas.rate_limits import RateLimitSetRequest

    actor = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    svc = _rl_service()

    from unittest.mock import AsyncMock, patch

    with patch("app.services.rate_limit_service.audit_logger.log", new=AsyncMock()) as audit:
        out = await svc.delete_user_rate_limit(session, user_id=U_A, actor=actor)
    await session.commit()
    assert out.deactivated == 1
    # 감사 기록이 지운 값(시드의 rpm=60 개인 한도)을 실제 행에서 읽어 남긴다.
    assert audit.await_args.kwargs["changes"]["before"] == {
        "model_alias": None, "rpm": 60, "tpm": None, "cpm": None, "cph": None,
    }
    assert await _active_rows(session, "USER", U_A) == []
    node = _find(await _tree(session), str(U_A))
    assert node.inherited_from == str(SEED_TEAM_ID), "제거 후 팀 상속으로 돌아오지 않았다"

    again = await svc.delete_user_rate_limit(session, user_id=U_A, actor=actor)
    await session.commit()
    assert again.deactivated == 0, "멱등이 아니다"

    # 제거 후 재설정 — 0024 의 부분 유니크 인덱스와 충돌하지 않아야 한다.
    await svc.set_user_rate_limit(session, user_id=U_A, data=RateLimitSetRequest(rpm=30), actor=actor)
    await session.commit()
    assert [(r.rpm_limit,) for r in await _active_rows(session, "USER", U_A)] == [(30,)]


async def test_global_limits_are_per_model(session):
    """한 모델의 GLOBAL 저장이 다른 모델의 GLOBAL 한도를 해제하지 않는다(0046) — 실제 행과 트리로 본다.

    시드의 RETIRED_ALIAS(비활성 모델, TPM 만 걸림)가 GLOBAL 한도를 갖고 있는 상태에서 다른 모델을
    저장한다. 둘 다 활성이고, 트리는 두 노드에 각자의 값을 싣는다. 대조군: 단일 슬롯 코드는 이 저장에
    409 를 냈고(동의 없음), 동의하면 RETIRED_ALIAS 의 행을 비활성화했다 — 어느 쪽이든 이 테스트에서
    실패한다.
    """
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.schemas.rate_limits import RateLimitSetRequest

    actor = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    before = await _active_rows(session, "GLOBAL")
    assert [(r.model_alias, r.tpm_limit) for r in before] == [(RETIRED_ALIAS, 5000)], "시드 전제가 깨졌다"

    await _rl_service().set_global_rate_limit(
        session, model_alias="claude-opus-4-7", data=RateLimitSetRequest(rpm=10), actor=actor
    )
    await session.commit()

    after = sorted((r.model_alias, r.rpm_limit, r.tpm_limit) for r in await _active_rows(session, "GLOBAL"))
    assert after == [("claude-opus-4-7", 10, None), (RETIRED_ALIAS, None, 5000)]
    nodes = await _tree(session)
    assert (_find(nodes, "claude-opus-4-7").config.rpm, _find(nodes, RETIRED_ALIAS).config.tpm) == (10, 5000)


async def test_clearing_one_models_global_limit_leaves_the_others(session):
    """콘솔의 '값을 비우고 저장' 이 GLOBAL 한도를 지우는 방법이다(GLOBAL 은 상속이 없어 비운 행 = 한도
    없음). 그 저장은 그 모델의 행만 바꾼다 — 다른 모델의 한도는 값까지 그대로다.

    대조군: 단일 슬롯 코드에서는 비우는 저장도 '다른 모델에 저장' 이라 나머지 GLOBAL 행을 전부
    비활성화했다(동의 없이는 409).
    """
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.schemas.rate_limits import RateLimitSetRequest

    actor = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    svc = _rl_service()
    await svc.set_global_rate_limit(
        session, model_alias="claude-sonnet-4-6", data=RateLimitSetRequest(tpm=300000), actor=actor
    )
    await svc.set_global_rate_limit(
        session, model_alias="claude-opus-4-7", data=RateLimitSetRequest(rpm=20), actor=actor
    )
    await session.commit()

    await svc.set_global_rate_limit(
        session, model_alias="claude-opus-4-7", data=RateLimitSetRequest(), actor=actor
    )
    await session.commit()

    rows = sorted((r.model_alias, r.rpm_limit, r.tpm_limit) for r in await _active_rows(session, "GLOBAL"))
    assert rows == [
        ("claude-opus-4-7", None, None),  # 비웠다 — 아무것도 집행하지 않는 행 하나
        ("claude-sonnet-4-6", None, 300000),
        (RETIRED_ALIAS, None, 5000),
    ]
    nodes = await _tree(session)
    assert _find(nodes, "claude-sonnet-4-6").config.tpm == 300000
    assert (_find(nodes, "claude-opus-4-7").config.rpm, _find(nodes, "claude-opus-4-7").config.tpm) == (None, None)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 예산 금액 상한 = 컬럼 폭
# ─────────────────────────────────────────────────────────────────────────────


async def _insert_budget(owned_dsn, amount: str):
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_asyncpg_dsn(owned_dsn))
    try:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO budget.budget_configs "
                "(id, scope, scope_id, max_budget_usd, policy, allocated_by, effective_from, is_active) "
                # is_active=false — 활성 행 유니크 인덱스(0024)와 다른 증명에 영향을 주지 않는다.
                "VALUES (gen_random_uuid(), 'USER'::budget.budget_scope, gen_random_uuid(), "
                "        $1, 'HARD_BLOCK'::budget.budget_policy, $2, CURRENT_DATE, false)",
                Decimal(amount),
                SEED_ADMIN_ID,
            )
    finally:
        await conn.close()


@pytest.mark.parametrize("amount", ["99999999.9999", "0", "0.0001"])
async def test_schema_accepted_amounts_fit_the_column(owned_dsn, amount):
    from app.schemas.budgets import SetBudgetRequest

    SetBudgetRequest(max_budget_usd=Decimal(amount))  # 스키마 통과
    await _insert_budget(owned_dsn, amount)  # 컬럼도 받는다


@pytest.mark.parametrize("amount", ["100000000", "100000000.0000", "123456789.5"])
async def test_schema_rejected_amounts_also_overflow_the_column(owned_dsn, amount):
    """스키마가 거부하는 값은 컬럼에서도 overflow 다 — 즉 422 가 막는 것은 진짜 500 이다."""
    asyncpg = pytest.importorskip("asyncpg")
    from pydantic import ValidationError

    from app.schemas.budgets import SetBudgetRequest

    with pytest.raises(ValidationError):
        SetBudgetRequest(max_budget_usd=Decimal(amount))
    with pytest.raises(asyncpg.exceptions.NumericValueOutOfRangeError):
        await _insert_budget(owned_dsn, amount)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 채팅 done 프레임의 durationMs ↔ chat_agent.messages.duration_ms
# ─────────────────────────────────────────────────────────────────────────────


class _Body:
    """botocore StreamingBody 대역 — AgentCore 가 흘리는 `data: <json>` 라인."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def iter_lines(self, chunk_size: int = 1024):  # noqa: ARG002 - 시그니처 호환
        yield from self._lines


async def test_assistant_message_persists_the_done_frame_duration(monkeypatch):
    import json as _json

    import boto3
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.routers import chat_agent as m

    engine = create_async_engine(PROOF_DSN)
    sid = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            if await conn.scalar(text("SELECT to_regclass('chat_agent.messages')")) is None:
                pytest.skip("chat_agent 스키마가 없다 — PROOF_DSN DB 에 alembic upgrade head 를 먼저")
            await conn.execute(
                text("INSERT INTO chat_agent.sessions (id, user_id) VALUES (:id, :uid)"),
                {"id": sid, "uid": SEED_ADMIN_ID},
            )

        lines = [
            f"data: {_json.dumps({'type': 'text', 'chunk': '답'})}".encode(),
            f"data: {_json.dumps({'type': 'done', 'session_id': 's', 'phase': 4})}".encode(),
        ]
        client = type("C", (), {"invoke_agent_runtime": lambda self, **_k: {"response": _Body(lines)}})()
        monkeypatch.setattr(boto3, "client", lambda *_a, **_k: client)
        monkeypatch.setattr(m, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))

        relay = m._StreamRelay()
        await m._agentcore_producer(relay, str(sid), "질문")

        done = [f.decode() for f in relay.frames if f.decode().startswith("event: done")]
        assert len(done) == 1, relay.frames
        frame_ms = _json.loads(done[0].split("data:", 1)[1].strip())["durationMs"]

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT content, duration_ms FROM chat_agent.messages "
                        "WHERE session_id = :sid AND role = 'assistant'"
                    ),
                    {"sid": sid},
                )
            ).one()
        assert row.content == "답"
        assert row.duration_ms == frame_ms, "라이브 footer 와 복원 footer 가 다른 숫자를 보인다"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM chat_agent.sessions WHERE id = :id"), {"id": sid})
        await engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# 5. 모니터링 모델 상태 — 등록부 LEFT JOIN
# ─────────────────────────────────────────────────────────────────────────────


async def test_monitoring_status_reflects_the_registry(owned_dsn):
    """최근 1시간 트래픽: 활성 alias / 비활성 alias(시드에서 INACTIVE 로 내림) / 등록부에 없는 alias."""
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.routers.monitoring import UNREGISTERED_MODEL_STATUS, monitoring_models

    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_asyncpg_dsn(owned_dsn))
    try:
        dept_id = await conn.fetchval("SELECT dept_id FROM auth.teams WHERE id = $1", SEED_TEAM_ID)
        for alias in ("claude-opus-4-8", RETIRED_ALIAS, "ghost-model-proof", "ghost-model-proof"):
            await conn.execute(
                "INSERT INTO usage.usage_logs "
                "(id, request_id, user_id, team_id, dept_id, model_alias, provider, status, "
                " requested_at, completed_at, cost_usd, input_tokens, output_tokens, latency_ms) "
                "VALUES (gen_random_uuid(), gen_random_uuid()::text, $1, $2, $3, $4, 'BEDROCK', "
                "        'SUCCESS'::usage.usage_status, now() - interval '5 minutes', now(), 0.01, 1, 1, 50)",
                U_A, SEED_TEAM_ID, dept_id, alias,
            )
    finally:
        await conn.close()

    engine = create_async_engine(owned_dsn)
    try:
        async with async_sessionmaker(engine)() as session:
            out = await monitoring_models(request=MagicMock(), admin=MagicMock(), session=session)
    finally:
        await engine.dispose()

    by_alias = {m["alias"]: m for m in out["models"]}
    assert by_alias["claude-opus-4-8"]["status"] == "ACTIVE"
    assert by_alias[RETIRED_ALIAS]["status"] == "INACTIVE", "비활성 모델이 ACTIVE 로 보인다"
    ghost = by_alias.get("ghost-model-proof")
    assert ghost is not None, "등록부에 없는 alias 의 트래픽이 조인에서 사라졌다"
    assert ghost["status"] == UNREGISTERED_MODEL_STATUS
    assert ghost["last_1h_requests"] == 2, "GROUP BY 가 status 때문에 행을 쪼개거나 합쳤다"


# ─────────────────────────────────────────────────────────────────────────────
# 6. 대시보드 top-teams — 동명 팀
# ─────────────────────────────────────────────────────────────────────────────


async def test_top_teams_keeps_same_named_teams_apart(owned_dsn):
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.routers.dashboard import top_teams

    twin_1, twin_2 = uuid.uuid4(), uuid.uuid4()
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_asyncpg_dsn(owned_dsn))
    try:
        dept_id = await conn.fetchval("SELECT dept_id FROM auth.teams WHERE id = $1", SEED_TEAM_ID)
        for tid in (twin_1, twin_2):
            await conn.execute(
                "INSERT INTO auth.teams (id, dept_id, name) VALUES ($1, $2, 'Twin Team')", tid, dept_id
            )
        # 다른 증명(2026-09)과 섞이지 않게 2026-07 에 쌓는다.
        for tid, cost in ((twin_1, "500.00"), (twin_2, "400.00")):
            await conn.execute(
                "INSERT INTO usage.usage_logs "
                "(id, request_id, user_id, team_id, dept_id, model_alias, provider, status, "
                " requested_at, completed_at, cost_usd, input_tokens, output_tokens, latency_ms) "
                "VALUES (gen_random_uuid(), gen_random_uuid()::text, $1, $2, $3, $4, 'BEDROCK', "
                "        'SUCCESS'::usage.usage_status, $5, $5, $6, 1, 1, 50)",
                U_A, tid, dept_id, ALIAS, _kst(7, 10, 12), Decimal(cost),
            )
    finally:
        await conn.close()

    engine = create_async_engine(owned_dsn)
    try:
        async with async_sessionmaker(engine)() as session:
            out = await top_teams(period="2026-07", limit=5, client=None, _admin=MagicMock(), session=session)
    finally:
        await engine.dispose()

    twins = [t for t in out["teams"] if t["name"] == "Twin Team"]
    assert [(t["team_id"], t["cost_usd"]) for t in twins] == [(str(twin_1), 500.0), (str(twin_2), 400.0)], (
        "동명 팀이 하나로 합쳐졌거나 team_id 가 없다 — 콘솔은 이 id 로 소진율을 조인한다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. 자동 하향 끄기
# ─────────────────────────────────────────────────────────────────────────────


async def test_downgrade_enabled_false_really_turns_it_off(session):
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text

    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.models.budget import BudgetScope
    from app.schemas.budgets import AutoDowngradeConfigRequest
    from app.services.budget_service import BudgetService

    cache = MagicMock()
    cache.invalidate = AsyncMock()
    svc = BudgetService(cache_mgr=cache)
    actor = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    rules = [{"from_model_alias": "claude-opus-4-8", "to_model_alias": "claude-sonnet-4-6", "threshold_pct": 80}]

    async def active_rules() -> int:
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM budget.downgrade_policies "
                    "WHERE scope = 'TEAM' AND scope_id = :sid AND is_active"
                ),
                {"sid": SEED_TEAM_ID},
            )
        ).scalar_one()

    # 대조군 — 켜서 저장하면 활성 규칙이 생긴다(시드 팀에는 03_seed_data 의 팀 예산이 있다).
    on = await svc.set_downgrade_config(
        session,
        scope=BudgetScope.TEAM,
        scope_id=SEED_TEAM_ID,
        data=AutoDowngradeConfigRequest(enabled=True, rules=rules),
        actor=actor,
    )
    await session.commit()
    assert on.enabled is True and await active_rules() == 1

    # 콘솔은 체크를 끈 상태에서도 규칙과 함께 PUT 한다(AutoDowngradeConfig.handleSave).
    from unittest.mock import patch

    with patch("app.services.budget_service.audit_logger.log", new=AsyncMock()) as audit:
        off = await svc.set_downgrade_config(
            session,
            scope=BudgetScope.TEAM,
            scope_id=SEED_TEAM_ID,
            data=AutoDowngradeConfigRequest(enabled=False, rules=rules),
            actor=actor,
        )
    await session.commit()
    assert off.enabled is False
    assert await active_rules() == 0, "끄기 저장 후에도 활성 규칙이 남아 gateway-proxy 가 계속 바꿔치기한다"
    # 끈 규칙은 DB 에 활성으로 남지 않는다 — 감사 기록(before)이 무엇을 껐는지 담는다.
    assert audit.await_args.kwargs["changes"]["before"] == {"rules": rules}

    # 규칙 없는 끄기 본문(`{"enabled": false, "rules": []}`)도 같은 결과다 — 예전엔 스키마가 422 로 막았다.
    await svc.set_downgrade_config(
        session, scope=BudgetScope.TEAM, scope_id=SEED_TEAM_ID,
        data=AutoDowngradeConfigRequest(enabled=True, rules=rules), actor=actor,
    )
    await session.commit()
    assert await active_rules() == 1
    off_empty = await svc.set_downgrade_config(
        session, scope=BudgetScope.TEAM, scope_id=SEED_TEAM_ID,
        data=AutoDowngradeConfigRequest.model_validate({"enabled": False, "rules": []}), actor=actor,
    )
    await session.commit()
    assert off_empty.enabled is False and await active_rules() == 0


# ─────────────────────────────────────────────────────────────────────────────
# 8. analytics scope — 닫힌 집합
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scope", ["xyz", "ALL", " all", "team:not-a-uuid"])
async def test_leader_malformed_scope_never_reaches_org_wide_data(session, scope):
    from app.core.auth import CurrentUser
    from app.core.exceptions import ValidationError
    from app.models.auth import UserRole
    from app.services.analytics_service import AnalyticsService

    leader = CurrentUser(user_id=uuid.uuid4(), email="lead@example.com", role=UserRole.TEAM_LEADER, team_id=SEED_TEAM_ID)
    svc = AnalyticsService()
    with pytest.raises(ValidationError):
        await svc.get_analytics(session, period=PERIOD, group_by="user", scope=scope, actor=leader)
    with pytest.raises(ValidationError):
        await svc.export_analytics(session, format="csv", period=PERIOD, group_by="user", scope=scope, actor=leader)


async def test_leader_all_is_scoped_to_the_own_team(session):
    """대조군 — 'all' 은 TEAM_LEADER 에게 자기 팀이다: 다른 팀(TEAM_B) 사용자는 화면에도 CSV 에도 없다."""
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.services.analytics_service import AnalyticsService

    leader = CurrentUser(user_id=uuid.uuid4(), email="lead@example.com", role=UserRole.TEAM_LEADER, team_id=SEED_TEAM_ID)
    svc = AnalyticsService()
    resp = await svc.get_analytics(session, period=PERIOD, group_by="user", scope="all", actor=leader)
    emails = {u.email for u in resp.by_user}
    assert "range-b@example.com" not in emails, "다른 팀 사용자가 리더 화면에 나왔다"
    assert "range-a@example.com" in emails
    assert resp.cost_summary.total_cost_usd == Decimal("71.00")  # 시드 팀만: 1+2+4+64 (TEAM_B 의 8 제외)
    csv_body, _ = await svc.export_analytics(
        session, format="csv", period=PERIOD, group_by="user", scope="all", actor=leader
    )
    assert "range-b@example.com" not in csv_body


# ─────────────────────────────────────────────────────────────────────────────
# 9. 팀 예산 배정 — 팀 밖 사용자
# ─────────────────────────────────────────────────────────────────────────────


async def _user_budget(session, user_id) -> Decimal | None:
    from sqlalchemy import text

    return (
        await session.execute(
            text(
                "SELECT max_budget_usd FROM budget.budget_configs "
                "WHERE scope = 'USER' AND scope_id = :u AND client IS NULL AND is_active"
            ),
            {"u": user_id},
        )
    ).scalar_one_or_none()


async def test_leader_cannot_overwrite_another_teams_user_budget(session):
    from unittest.mock import AsyncMock, MagicMock

    from app.core.auth import CurrentUser
    from app.core.exceptions import ForbiddenError, ValidationError
    from app.models.auth import UserRole
    from app.schemas.budgets import AllocateBudgetItem, AllocateBudgetRequest, SetBudgetRequest
    from app.services.budget_service import BudgetService

    cache = MagicMock()
    cache.invalidate = AsyncMock()
    cache._redis = AsyncMock()
    svc = BudgetService(cache_mgr=cache)
    admin = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    # 리더 신원은 실재하는 시드 팀 사용자여야 한다(budget_configs.allocated_by FK).
    leader = CurrentUser(user_id=U_SEED, email="range-seed@example.com", role=UserRole.TEAM_LEADER, team_id=SEED_TEAM_ID)

    # TEAM_B 사용자에게 개인 예산 $100 이 있다 — 이 값이 리더의 시도 뒤에도 그대로여야 한다.
    await svc.set_user_budget(session, user_id=U_B, data=SetBudgetRequest(max_budget_usd=Decimal("100")), actor=admin)
    await session.commit()
    assert await _user_budget(session, U_B) == Decimal("100")

    attack = AllocateBudgetRequest(allocations=[
        AllocateBudgetItem(user_id=str(U_A), allocated_usd=Decimal("10")),  # 자기 팀 멤버
        AllocateBudgetItem(user_id=str(U_B), allocated_usd=Decimal("0")),  # 다른 팀 사용자
    ])
    with pytest.raises(ForbiddenError):
        await svc.allocate_team_budget(session, team_id=SEED_TEAM_ID, data=attack, actor=leader)
    await session.rollback()
    assert await _user_budget(session, U_B) == Decimal("100"), "다른 팀 사용자의 예산이 덮어써졌다"

    with pytest.raises(ValidationError):  # 관리자라도 팀 밖 사용자는 이 팀의 배정 대상이 아니다
        await svc.allocate_team_budget(session, team_id=SEED_TEAM_ID, data=attack, actor=admin)
    await session.rollback()

    # 대조군 — 자기 팀 멤버만이면 배정된다.
    ok = AllocateBudgetRequest(allocations=[AllocateBudgetItem(user_id=str(U_A), allocated_usd=Decimal("10"))])
    await svc.allocate_team_budget(session, team_id=SEED_TEAM_ID, data=ok, actor=leader)
    await session.commit()
    assert await _user_budget(session, U_A) == Decimal("10")


async def test_teamless_leader_cannot_manage_a_teamless_users_budget(session):
    """팀 없는 TEAM_LEADER 와 팀 없는 사용자(auth.users.team_id IS NULL) — 예전 가드
    `user.team_id != actor.team_id` 는 None != None 이 False 라 통과했고, 리더가 그 사용자의 예산을 썼다.

    리더 신원은 이 트랜잭션 안에서 만든 실제 사용자 행이다(budget_configs.allocated_by FK) — 그래야 예전
    코드에서 쓰기가 FK 오류가 아니라 **성공**해 이 테스트가 정확한 이유로 실패한다. 끝에 되돌린다."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text

    from app.core.auth import CurrentUser
    from app.core.exceptions import ForbiddenError
    from app.models.auth import UserRole
    from app.schemas.budgets import SetBudgetRequest
    from app.services.budget_service import BudgetService

    cache = MagicMock()
    cache.invalidate = AsyncMock()
    cache._redis = AsyncMock()
    svc = BudgetService(cache_mgr=cache)
    leader_id = uuid.uuid4()
    leader = CurrentUser(user_id=leader_id, email="noteam-leader@example.com", role=UserRole.TEAM_LEADER, team_id=None)
    before = await _user_budget(session, U_TEAMLESS)
    try:
        await session.execute(
            text(
                "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
                "VALUES (:u, NULL, 'noteam-leader@example.com', 'noteam-leader', 'TEAM_LEADER', :sub)"
            ),
            {"u": leader_id, "sub": f"sub-{leader_id}"},
        )
        with pytest.raises(ForbiddenError):
            await svc.set_user_budget(
                session, user_id=U_TEAMLESS, data=SetBudgetRequest(max_budget_usd=Decimal("999")), actor=leader
            )
        with pytest.raises(ForbiddenError):
            await svc.get_user_app_budgets(session, user_id=U_TEAMLESS, actor=leader)
        assert await _user_budget(session, U_TEAMLESS) == before, "팀 없는 리더가 팀 없는 사용자의 예산을 바꿨다"
    finally:
        await session.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# 10. /admin/my/budget — 팀 예산 상태와 $0 한도
# ─────────────────────────────────────────────────────────────────────────────


async def _my_budget(session, user_id):
    from unittest.mock import MagicMock

    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.routers.my import get_my_budget

    # JWT 의 team_id 는 일부러 비운다 — 라우트는 DB 의 현재 소속을 읽어야 한다(게이트웨이도 그렇다).
    me = CurrentUser(user_id=user_id, email="me@example.com", role=UserRole.DEVELOPER, team_id=None)
    request = MagicMock()
    # 이 절은 DB 경로(budget_usages)를 본다 — Redis 카운터 경로는 test_high_my_budget_gateway_parity_real.py.
    request.app.state.redis = None
    return await get_my_budget(request=request, user=me, session=session)


async def test_my_budget_reports_the_team_budget_the_gateway_will_check(session):
    """게이트웨이는 팀 예산이 없으면 거부한다(team_budget_unset). 카드가 "팀 예산 적용" 이라고 말하려면
    그게 사실이어야 한다 — 시드 팀(팀 예산 있음) / TEAM_B(없음) / 팀 미배정을 실제 행으로 가른다."""
    assert (await _my_budget(session, U_A))["team"] == {
        "assigned": True, "budget_configured": True, "budget_exhausted": False,
    }
    assert (await _my_budget(session, U_B))["team"] == {
        "assigned": True, "budget_configured": False, "budget_exhausted": False,
    }
    assert (await _my_budget(session, U_TEAMLESS))["team"] == {
        "assigned": False, "budget_configured": False, "budget_exhausted": False,
    }


async def test_my_budget_zero_limit_is_configured_not_unset(session):
    """$0 HARD_BLOCK 은 모든 요청을 막는다 — 이전 필드로는 미설정과 같아 보이므로(하위 호환으로 사용률도 0)
    configured=true 가 그 차이를 실어야 한다."""
    from unittest.mock import AsyncMock, MagicMock

    from app.core.auth import CurrentUser
    from app.models.auth import UserRole
    from app.schemas.budgets import SetBudgetRequest
    from app.services.budget_service import BudgetService

    cache = MagicMock()
    cache.invalidate = AsyncMock()
    cache._redis = AsyncMock()
    admin = CurrentUser(user_id=SEED_ADMIN_ID, email="admin@example.com", role=UserRole.ADMIN, team_id=None)
    await BudgetService(cache_mgr=cache).set_user_budget(
        session, user_id=U_TEAMLESS, data=SetBudgetRequest(max_budget_usd=Decimal("0")), actor=admin
    )
    await session.commit()

    b = (await _my_budget(session, U_TEAMLESS))["budget"]
    assert (b["limit_usd"], b["usage_pct"], b["policy"], b["configured"]) == (0, 0, "HARD_BLOCK", True)


async def test_my_budget_keeps_mains_values_on_real_rows(session):
    """하위 호환을 실제 행으로 — 이전 카드(main)가 읽는 값이 main 의 실제 응답과 같다(admin-api 를 먼저 배포한
    동안 이전 카드가 그대로 그린다). 기준은 admin-ui 픽스처의 ``main`` — main(e7a1588)의 라우트가 낸 값이다.
    새 의미는 configured 로만 갈린다: 미설정과 $0 하드 차단은 이전 필드로는 같아 보인다."""
    import json
    from pathlib import Path

    from sqlalchemy import text

    fixture = Path(__file__).resolve().parents[3] / "admin-ui" / "tests" / "fixtures" / "my-budget.json"
    if not fixture.exists():
        pytest.skip(f"sibling admin-ui not in this checkout: {fixture}")
    main = json.loads(fixture.read_text(encoding="utf-8"))["main"]

    cases = {  # 상태 → (개인 예산 (한도, 정책) | None, 이번 달 지출) — 픽스처의 같은 상태와 같은 입력
        "no_budget": (None, "12.34"),
        "zero_hard_block": (("0", "HARD_BLOCK"), "1.00"),
        "normal": (("100", "SOFT_WARNING"), "25.00"),
    }
    dept_id = (await session.execute(text("SELECT dept_id FROM auth.teams WHERE id = :t"), {"t": SEED_TEAM_ID})).scalar_one()
    provider = (
        await session.execute(text("SELECT provider::text FROM model.model_aliases WHERE alias = :a"), {"a": ALIAS})
    ).scalar_one()
    seen = {}
    try:
        for state, (cfg, used) in cases.items():
            uid = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
                    "VALUES (:u, :t, :e, :e, 'DEVELOPER', :sub)"
                ),
                {"u": uid, "t": SEED_TEAM_ID, "e": f"compat-{state}@example.com", "sub": f"sub-{uid}"},
            )
            if cfg is not None:
                await session.execute(
                    text(
                        "INSERT INTO budget.budget_configs "
                        "(scope, scope_id, max_budget_usd, policy, allocated_by, effective_from) "
                        "VALUES ('USER', :u, :l, CAST(:p AS budget.budget_policy), :a, CURRENT_DATE)"
                    ),
                    {"u": uid, "l": Decimal(cfg[0]), "p": cfg[1], "a": SEED_ADMIN_ID},
                )
            await session.execute(
                text(
                    "INSERT INTO usage.usage_logs (id, request_id, user_id, team_id, dept_id, model_alias, provider, "
                    "status, requested_at, completed_at, cost_usd, input_tokens, output_tokens, cache_read_tokens, "
                    "cache_creation_tokens, latency_ms) VALUES (gen_random_uuid(), gen_random_uuid()::text, :u, :t, "
                    ":d, :m, :pv, 'SUCCESS', now(), now(), :c, 10, 20, 0, 0, 100)"
                ),
                {"u": uid, "t": SEED_TEAM_ID, "d": dept_id, "m": ALIAS, "pv": provider, "c": Decimal(used)},
            )
            seen[state] = (await _my_budget(session, uid))["budget"]
    finally:
        await session.rollback()

    for state, budget in seen.items():
        legacy = {k: budget[k] for k in main[state]["budget"]}
        assert legacy == main[state]["budget"], f"{state}: 이전 카드가 읽는 값이 main 과 다르다 {legacy}"
        assert all(isinstance(budget[k], (int, float)) for k in ("limit_usd", "used_usd", "remaining_usd", "usage_pct"))
    assert (seen["no_budget"]["configured"], seen["zero_hard_block"]["configured"]) == (False, True)


def _previous_period(period: str) -> str:
    year, month = map(int, period.split("-"))
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


@pytest.mark.parametrize(
    ("policy", "limit", "used_now", "used_last_month", "exhausted"),
    [
        ("HARD_BLOCK", "100", "100", None, True),
        ("HARD_BLOCK", "100", "99.99", None, False),
        ("HARD_BLOCK", "0", None, None, True),  # $0 — 사용 행이 아예 없어도 거부된다
        ("SOFT_WARNING", "100", "110", None, True),
        ("SOFT_WARNING", "100", "109.99", None, False),
        ("THROTTLE", "100", "500", None, False),  # THROTTLE 은 거부하지 않는다(속도만 줄인다)
        ("HARD_BLOCK", "100", None, "500", False),  # 지난달 사용액은 이번 달을 소진시키지 않는다
    ],
)
async def test_my_budget_reports_an_exhausted_team_budget(
    session, policy, limit, used_now, used_last_month, exhausted
):
    """팀 예산 행이 **있어도** 게이트웨이가 거부하는 상태를 카드가 알아야 한다 — 실제 budget_configs ·
    budget_usages 행(이번 KST 월, TEAM, client IS NULL)으로 가른다. 게이트웨이의 팀 계층 규칙은
    budget_check.lua 그대로다(HARD_BLOCK used>=limit · SOFT_WARNING used*100>=limit*110 · THROTTLE 거부 없음).
    행은 이 트랜잭션 안에서만 만들고 되돌린다 — 모듈 공유 DB 의 다른 증명 숫자를 움직이지 않는다."""
    from sqlalchemy import text

    from app.core.usage_filters import current_kst_period

    period = current_kst_period()
    team_id, user_id = uuid.uuid4(), uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO auth.teams (id, dept_id, name) "
                "SELECT :t, dept_id, 'Exhaust Team' FROM auth.teams WHERE id = :seed"
            ),
            {"t": team_id, "seed": SEED_TEAM_ID},
        )
        await session.execute(
            text(
                "INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) "
                "VALUES (:u, :t, 'exhaust@example.com', 'exhaust', 'DEVELOPER', :sub)"
            ),
            {"u": user_id, "t": team_id, "sub": f"sub-{user_id}"},
        )
        await session.execute(
            text(
                "INSERT INTO budget.budget_configs "
                "(scope, scope_id, max_budget_usd, policy, allocated_by, effective_from) "
                "VALUES ('TEAM', :t, :limit, CAST(:policy AS budget.budget_policy), :admin, CURRENT_DATE)"
            ),
            {"t": team_id, "limit": Decimal(limit), "policy": policy, "admin": SEED_ADMIN_ID},
        )
        for p, used in ((period, used_now), (_previous_period(period), used_last_month)):
            if used is not None:
                await session.execute(
                    text(
                        "INSERT INTO budget.budget_usages (scope, scope_id, period, client, used_usd, limit_usd) "
                        "VALUES ('TEAM', :t, :p, NULL, :used, :limit)"
                    ),
                    {"t": team_id, "p": p, "used": Decimal(used), "limit": Decimal(limit)},
                )

        team = (await _my_budget(session, user_id))["team"]
    finally:
        await session.rollback()

    assert team == {"assigned": True, "budget_configured": True, "budget_exhausted": exhausted}


# ─────────────────────────────────────────────────────────────────────────────
# 11. TEAM_LEADER 의 예산 읽기 범위 — 배정·요약·자동 하향 (실제 라우터 + 실 PG, 끝에 롤백)
# ─────────────────────────────────────────────────────────────────────────────
#
# 세 GET 은 require_admin_or_team_leader 만 걸려 있었고 호출자를 서비스에 넘기지 않았다. 그래서 리더가
# 다른 팀의 배정(팀 총액·멤버 이름·개인 배정액), 전사 예산 요약(다른 팀·팀 없는 사용자 포함), 다른 팀의
# 자동 하향 규칙을 읽었다(관리자 가이드는 '자기 팀만' 이라고 적는다). 상태 코드까지 봐야 하므로 실제
# budgets 라우터와 실제 예외 처리기를 띄운다. 세션은 바깥 트랜잭션의 savepoint 라 라우트가 커밋해도 끝에
# 전부 되돌아간다 — 모듈이 공유하는 DB 의 다른 증명(10절의 TEAM_B '예산 없음' 등)을 움직이지 않는다.


def _actor(role: str, team_id):
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole

    return CurrentUser(user_id=uuid.uuid4(), email="actor@example.com", role=UserRole(role), team_id=team_id)


@pytest.fixture
async def budget_routes(owned_dsn):
    """``async with budget_routes(actor) as client`` — 같은 트랜잭션 안의 실제 budgets 라우터."""
    import contextlib
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core.auth import get_current_user
    from app.core.db import get_db_session
    from app.main import register_exception_handlers
    from app.routers import budgets
    from app.services.budget_service import BudgetService

    engine = create_async_engine(owned_dsn)

    @contextlib.asynccontextmanager
    async def _open(actor):
        async with engine.connect() as conn:
            trans = await conn.begin()
            session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
            # 다른 팀(TEAM_B)에 읽을 거리가 있어야 '못 읽는다' 가 공허하지 않다 — 팀 예산, 멤버 개인 예산,
            # 팀·멤버 자동 하향 규칙. 활성 config 는 (scope, scope_id, client) 당 하나라 먼저 비활성화한다.
            for scope, sid, amount in (("TEAM", TEAM_B_ID, 777), ("USER", U_B, 123), ("TEAM", SEED_TEAM_ID, None)):
                if amount is None:
                    continue
                await session.execute(
                    text(
                        "UPDATE budget.budget_configs SET is_active = false "
                        "WHERE scope = CAST(:s AS budget.budget_scope) AND scope_id = :i AND client IS NULL AND is_active"
                    ),
                    {"s": scope, "i": sid},
                )
                await session.execute(
                    text(
                        "INSERT INTO budget.budget_configs "
                        "(scope, scope_id, max_budget_usd, policy, allocated_by, effective_from) "
                        "VALUES (CAST(:s AS budget.budget_scope), :i, :a, 'HARD_BLOCK', :by, CURRENT_DATE)"
                    ),
                    {"s": scope, "i": sid, "a": amount, "by": SEED_ADMIN_ID},
                )
            for scope, sid in (("TEAM", TEAM_B_ID), ("USER", U_B), ("TEAM", SEED_TEAM_ID), ("USER", U_A)):
                await session.execute(
                    text(
                        "INSERT INTO budget.downgrade_policies "
                        "(scope, scope_id, threshold_pct, from_model_alias, to_model_alias, created_by) "
                        "SELECT CAST(:s AS budget.budget_scope), :i, 80, 'claude-sonnet-4-6', "
                        "       'claude-haiku-4-5-20251001', :by "
                        "WHERE NOT EXISTS (SELECT 1 FROM budget.downgrade_policies WHERE scope = CAST(:s AS "
                        "budget.budget_scope) AND scope_id = :i AND from_model_alias = 'claude-sonnet-4-6' AND is_active)"
                    ),
                    {"s": scope, "i": sid, "by": SEED_ADMIN_ID},
                )
            await session.flush()

            app = FastAPI()
            register_exception_handlers(app)
            app.include_router(budgets.router)
            cache = MagicMock()
            cache.invalidate = AsyncMock()
            cache._redis = AsyncMock()
            app.state.budget_service = BudgetService(cache_mgr=cache)
            app.state.redis = None
            app.dependency_overrides[get_current_user] = lambda: actor

            async def _db():
                yield session

            app.dependency_overrides[get_db_session] = _db
            try:
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://proof") as client:
                    yield client
            finally:
                await session.close()
                await trans.rollback()

    yield _open
    await engine.dispose()


def _summary_ids(resp) -> tuple[set[str], set[str], set[str | None]]:
    items = resp.json()["summary"]
    teams = {i["target_id"] for i in items if i["target_type"] == "team"}
    users = {i["target_id"] for i in items if i["target_type"] == "user"}
    user_teams = {i["team_id"] for i in items if i["target_type"] == "user"}
    return teams, users, user_teams


async def test_leader_reads_only_the_own_teams_allocation(budget_routes):
    async with budget_routes(_actor("TEAM_LEADER", SEED_TEAM_ID)) as client:
        other = await client.get(f"/admin/budgets/team/{TEAM_B_ID}/allocation")
        assert other.status_code == 403, f"리더가 다른 팀의 배정을 읽었다: {other.text[:300]}"
        assert "Range Team B" not in other.text and str(U_B) not in other.text

        own = await client.get(f"/admin/budgets/team/{SEED_TEAM_ID}/allocation")  # 대조군
        assert own.status_code == 200, own.text
        body = own.json()
        assert body["team_id"] == str(SEED_TEAM_ID)
        assert {str(U_A), str(U_SEED)} <= {e["target_id"] for e in body["entries"]}


async def test_leader_summary_is_the_own_team_view(budget_routes):
    """리더 화면(/budgets, 대시보드 팀 순위)이 이 응답을 쓴다 — 403 이 아니라 자기 팀으로 걸러서 준다."""
    from app.core.usage_filters import current_kst_period

    params = {"period": current_kst_period()}
    async with budget_routes(_actor("TEAM_LEADER", SEED_TEAM_ID)) as client:
        resp = await client.get("/admin/budgets/summary", params=params)
        assert resp.status_code == 200, resp.text
        teams, users, user_teams = _summary_ids(resp)
        assert teams == {str(SEED_TEAM_ID)}, f"다른 팀 행이 보인다: {teams}"
        assert user_teams == {str(SEED_TEAM_ID)}, f"다른 팀·팀 없는 사용자가 보인다: {user_teams}"
        assert {str(U_A), str(U_SEED)} <= users
        assert not {str(U_B), str(U_TEAMLESS)} & users

    async with budget_routes(_actor("ADMIN", None)) as client:  # 대조군 — 관리자는 전사를 본다
        resp = await client.get("/admin/budgets/summary", params=params)
        assert resp.status_code == 200, resp.text
        teams, users, _ = _summary_ids(resp)
        assert {str(SEED_TEAM_ID), str(TEAM_B_ID)} <= teams
        assert {str(U_A), str(U_B), str(U_TEAMLESS)} <= users


async def test_leader_cannot_read_another_teams_downgrade_rules(budget_routes):
    async with budget_routes(_actor("TEAM_LEADER", SEED_TEAM_ID)) as client:
        for path in (f"/admin/budgets/TEAM/{TEAM_B_ID}/downgrade", f"/admin/budgets/USER/{U_B}/downgrade"):
            resp = await client.get(path)
            assert resp.status_code == 403, f"{path} → {resp.status_code} {resp.text[:300]}"
            assert "claude-haiku" not in resp.text, "거부 응답에 다른 팀의 규칙이 실렸다"

        for path in (f"/admin/budgets/TEAM/{SEED_TEAM_ID}/downgrade", f"/admin/budgets/USER/{U_A}/downgrade"):
            resp = await client.get(path)  # 대조군 — 자기 팀과 자기 팀 멤버는 그대로
            assert resp.status_code == 200, f"{path} → {resp.status_code} {resp.text[:300]}"
            assert resp.json()["enabled"] is True


async def test_teamless_leader_reads_no_teams_budget_data(budget_routes):
    """팀 없는 리더 — None == None 으로 통과하는 길이 없어야 한다."""
    from app.core.usage_filters import current_kst_period

    async with budget_routes(_actor("TEAM_LEADER", None)) as client:
        assert (await client.get(f"/admin/budgets/team/{SEED_TEAM_ID}/allocation")).status_code == 403
        resp = await client.get("/admin/budgets/summary", params={"period": current_kst_period()})
        assert resp.status_code == 200, resp.text
        assert resp.json()["summary"] == [], "팀 없는 리더에게 누군가의 예산이 보인다"
        for path in (f"/admin/budgets/USER/{U_TEAMLESS}/downgrade", f"/admin/budgets/TEAM/{TEAM_B_ID}/downgrade"):
            assert (await client.get(path)).status_code == 403, path
