# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""워커가 청구 티어(context_tier)를 usage_logs 에 실제로 쓰는지 (실 PostgreSQL).

게이트웨이 resolve_context_tier 가 판정한 티어가 cost:stream 을 타고 와서
usage.usage_logs.context_tier 에 그대로 남아야 감사가 성립한다. INSERT 컬럼 목록과 ORM/
파라미터가 어긋나면(allowed_clients 사고 부류) 값이 조용히 유실된다 — 실 행을 읽어 막는다.

⚠️ INSERT 는 context_tier 를 **명시 컬럼**으로 이름 대므로, 마이그레이션 0039(컬럼 추가)가
   워커보다 먼저 적용돼야 한다. 이 테스트의 DB(PROOF_DSN)는 head=0039 를 가리킨다.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED = "_ctierproof"
USER_ID = "91111111-1111-1111-1111-111111111111"
TEAM_ID = "92222222-2222-2222-2222-222222222222"
DEPT_ID = "93333333-3333-3333-3333-333333333333"
ORG_ID = "94444444-4444-4444-4444-444444444444"

_pg = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PG 필요(0039 head).")


def _entry(rid, tier):
    from worker.schemas.cost_stream import CostStreamEntry

    return CostStreamEntry(
        request_id=rid, user_id=USER_ID, team_id=TEAM_ID, dept_id=DEPT_ID,
        model_alias="gpt-5.6-terra", provider="BEDROCK",
        input_tokens=300_000, output_tokens=100, cost_usd=Decimal("1.32"),
        latency_ms=100, status="SUCCESS", context_tier=tier,
        requested_at="2026-06-01T00:00:00+00:00", completed_at="2026-06-01T00:00:01+00:00",
        period="2026-06", date="2026-06-01",
    )


def _split(dsn):
    pre, _, name = dsn.rpartition("/")
    return pre, name


async def _make_db():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pre, name = _split(PROOF_DSN)
    owned = name + _OWNED
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp", "lct")), owned
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                 "WHERE datname=:d AND pid<>pg_backend_pid()"), {"d": name})
            await c.execute(text(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"'))
    finally:
        await admin.dispose()
    return f"{pre}/{owned}"


@pytest.fixture(scope="module")
async def dsn():
    asyncpg = pytest.importorskip("asyncpg")
    if not PROOF_DSN:
        pytest.skip("PROOF_DSN 미설정")
    url = await _make_db()
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute(
            "INSERT INTO auth.organizations (id,name) VALUES ($1,'ct-org') "
            "ON CONFLICT DO NOTHING", ORG_ID)
        await conn.execute(
            "INSERT INTO auth.departments (id,org_id,name) VALUES ($1,$2,'ct-d') "
            "ON CONFLICT DO NOTHING", DEPT_ID, ORG_ID)
        await conn.execute(
            "INSERT INTO auth.teams (id,dept_id,name) VALUES ($1,$2,'ct-t') "
            "ON CONFLICT DO NOTHING", TEAM_ID, DEPT_ID)
        await conn.execute(
            "INSERT INTO auth.users (id,team_id,email,display_name,role,sso_subject) "
            "VALUES ($1,$2,'ct@example.invalid','ct','ADMIN','ct-sub') "
            "ON CONFLICT DO NOTHING", USER_ID, TEAM_ID)
    finally:
        await conn.close()
    yield url
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    pre, name = _split(PROOF_DSN)
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{name}{_OWNED}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def _flush(engine, entries):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from worker.batch_flusher import BatchFlusher

    class _Pipe:
        def __getattr__(self, _n): return lambda *a, **k: None
        async def execute(self): return None

    class _Redis:
        def pipeline(self, *a, **k): return _Pipe()
        async def publish(self, *a, **k): return None

    fl = BatchFlusher(
        session_factory=async_sessionmaker(engine, expire_on_commit=False), redis=_Redis()
    )
    await fl.flush(entries)


@_pg
async def test_context_tier_is_persisted_verbatim(dsn):
    """⚠️ 게이트웨이가 판정한 티어가 usage_logs 에 그대로 남아야 한다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        ids = {t: f"{t}-{uuid.uuid4()}" for t in ("long", "short")}
        await _flush(engine, [_entry(ids["long"], "long"), _entry(ids["short"], "short")])
        async with engine.connect() as c:
            rows = dict((await c.execute(text(
                "SELECT request_id, context_tier FROM usage.usage_logs "
                "WHERE request_id = ANY(:i)"),
                {"i": list(ids.values())})).all())
        assert rows.get(ids["long"]) == "long", rows
        assert rows.get(ids["short"]) == "short", rows
    finally:
        await engine.dispose()


@_pg
async def test_null_tier_is_persisted_as_null(dsn):
    """⚠️ 대조군. 티어 없는 모델(None)은 NULL 로 남아야 한다 — 'short' 로 오기록되면 안 된다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(dsn)
    try:
        rid = f"none-{uuid.uuid4()}"
        await _flush(engine, [_entry(rid, None)])
        async with engine.connect() as c:
            got = (await c.execute(text(
                "SELECT context_tier FROM usage.usage_logs WHERE request_id=:r"),
                {"r": rid})).scalar_one()
        assert got is None, got
    finally:
        await engine.dispose()


def test_insert_names_context_tier_as_a_column():
    """⚠️ 구조 가드. INSERT 컬럼 목록에 context_tier 가 없으면 값이 조용히 유실된다."""
    src = (
        Path(__file__).resolve().parents[2] / "src" / "worker" / "batch_flusher.py"
    ).read_text(encoding="utf-8")
    assert "context_tier" in src, "batch_flusher 가 context_tier 를 다루지 않는다"
    # INSERT 컬럼 목록과 파라미터 바인딩 둘 다에 있어야 한다.
    assert ", context_tier" in src and ":context_tier" in src, "INSERT 컬럼/파라미터 누락"
