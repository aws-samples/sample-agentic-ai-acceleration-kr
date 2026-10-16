# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""0046 — GLOBAL rate limit 을 모델별로. 마이그레이션 함수·init SQL 을 실 PostgreSQL 에서 고정한다.

  1. ``db/init`` 이 만드는 rate-limit 인덱스·``rl_global_release_guard`` 트리거·함수와 0046 ``upgrade()`` 가
     만드는 것이 글자까지 같다(``pg_get_indexdef`` / ``pg_get_triggerdef`` / ``pg_get_functiondef``). init SQL
     은 매 마이그레이션 Job 에서 alembic 보다 먼저 돌고 compose·CI 는 init SQL 로 스키마를 만든다.
  2. 여러 모델이 실제 GLOBAL 한도를 가지면 ``downgrade()`` 는 그 목록과 정리 방법을 담아 실패하고 아무것도
     바꾸지 않는다(대조군: 판정이 없으면 0045 인덱스 생성이 PostgreSQL 의 중복 키 오류로 죽는다).
  3. 아무것도 집행하지 않는 행 — rpm·tpm 이 모두 NULL 이거나 alias 가 없는 행 — 은 판정에서 빠지고, 트리거를
     ``SET LOCAL`` 로 우회해 비활성화되며 감사 로그가 남는다(대조군: alias 없는 행을 세던 판정은 거부했다).
  4. 정리 SQL — 문서(06-upgrade-rollback §4.4)의 블록과 downgrade 거부 메시지의 SQL — 을 **그대로** 실행한다.
     남길 alias 에 실제 한도가 없으면(자리표시자를 그대로 두었거나 오타) 멈추고 아무것도 바꾸지 않는다
     (대조군: 예전 형태는 남길 한도까지 전부 해제하고 커밋했다). DO 블록 뒤에 psql 이 계속 가도 UPDATE 가 해제하지
     않는다. 맞는 alias 면 운영자가 COMMIT 한 뒤에 한도 하나가 남는다. 우회 없이는 커밋에서 거부되고(대조군),
     우회는 그 트랜잭션에만 걸린다.
  5. 0046 은 양방향 모두 ACCESS EXCLUSIVE 를 **먼저** 잡는다 — 이 테이블을 읽고 이어서 쓰는 트랜잭션이
     끝까지 간다(대조군: SHARE·SHARE ROW EXCLUSIVE 에서 올리던 순서는 그 트랜잭션을 교착으로 죽였다).
  6. init SQL 의 0046 블록은 객체가 있으면 잠그지 않고, 만들어야 할 때만 잠그며 그 대기는 lock_timeout 5s 로
     묶인다 — 넘으면 깨끗이 실패하고 다시 돌리면 된다. rate_limit_configs 에 쓰기가 열려 있어도 init SQL
     파일 전체가 기다리지 않는다(대조군: 맨 ``CREATE [UNIQUE] INDEX IF NOT EXISTS`` 는 인덱스가 있어도 열린
     쓰기 뒤에서 기다린다).
  7. init SQL 은 트리거를 새 설치(alembic_version 없음)와 이미 0046 이후인 DB 에만 만든다. 0046 에 닿기 전에
     멈춘 0042 DB 에서는 만들지 않으므로, 이전 admin-api 의 단일 슬롯 교체가 그대로 된다(대조군: 트리거가
     있으면 그 교체가 커밋에서 거부된다). alembic_version 은 alembic 과 같은 롤의 search_path 로 찾는다 — 롤
     이름의 스키마에 있어도 같다(대조군: public 으로 못박은 판정은 그 0042 DB 를 새 설치로 보고 트리거를 만들어
     슬롯 교체가 23514 로 거부됐다). 0024 이전 DB 가 이 차트의 Job 으로 0046 에 닿는지는 실 alembic 이 필요해
     CI pg-proof 잡의 "a DB below 0024" 스텝이 본다.
  8. 0046 이 USER/TEAM 인덱스의 **이름을 그대로** 쓰는 이유: 이전 차트의 init SQL 이 0046 DB 에서 다시 돌아도
     no-op 이다(대조군: 그 이름이 없으면 같은 문장이 단일 슬롯을 조용히 되살린다).

마이그레이션 함수는 alembic 없이 돈다(이 서비스의 lock 에 alembic 이 없다). 모듈의 ``from alembic import op``
자리에 ``execute`` / ``get_bind`` 만 가진 대역을 넣고, alembic 의 ``transaction_per_migration`` 처럼 한
트랜잭션에서 실행한다 — 예외면 롤백된다. 문자열 SQL 은 alembic 처럼 ``text()`` 로 감싼다. 실 alembic CLI
왕복은 CI 의 pg-proof 잡이 별도 DB 에서 돈다.

이전 릴리스 admin-api 의 저장(alias 무관 upsert)이 트리거에 막히는 것은
tests/regression/test_high_duplicate_active_config.py 가 실제 저장 경로로 본다.

⚠️ ``PROOF_DSN`` 의 DB 는 건드리지 않는다. 자기 소유 ``<dbname>_rl0046`` 을 init SQL 로 만들어 쓰고 지운다.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

from tests.regression.test_high_console_contract_real_db import (
    SEED_ADMIN_ID,
    _apply_init_sql,
    _split_dsn,
)
from tests.regression.test_high_duplicate_active_config import _executed_0024_index_sql

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED_SUFFIX = "_rl0046"
_PROJECT = Path(__file__).resolve().parents[3]
_M0046 = _PROJECT / "db" / "versions" / "0046_global_rate_limit_per_model.py"
_INIT_TABLES = _PROJECT / "db" / "init" / "02_create_tables.sql"
_ROLLBACK_DOC = _PROJECT / "deployment" / "docs" / "eks-fargate" / "06-upgrade-rollback.md"

pytestmark = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PostgreSQL 필요")

_UNIQUE_INDEXES = (
    "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index "
    "WHERE indrelid = 'model.rate_limit_configs'::regclass AND indisunique AND NOT indisprimary "
    "ORDER BY 1"
)
_GUARD = (
    "SELECT 'trigger', pg_get_triggerdef(oid) FROM pg_trigger "
    "WHERE tgrelid = 'model.rate_limit_configs'::regclass AND NOT tgisinternal "
    "UNION ALL "
    "SELECT 'function', pg_get_functiondef(p.oid) FROM pg_proc p "
    "JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname = 'model' AND p.proname = 'rl_global_release_guard' "
    "ORDER BY 1"
)
_ACTIVE_GLOBAL = (
    "SELECT model_alias, rpm_limit, tpm_limit FROM model.rate_limit_configs "
    "WHERE scope = 'GLOBAL' AND is_active ORDER BY model_alias NULLS FIRST"
)
_AUDITED_RELEASES = (
    "SELECT changes -> 'before' ->> 'model_alias', changes -> 'before' ->> 'rpm' FROM audit.audit_logs "
    "WHERE action = 'DELETE_RATE_LIMIT' AND resource_type = 'RateLimitConfig' "
    "ORDER BY 1 NULLS FIRST"
)


class _Op:
    """``alembic.op`` 중 0046 이 쓰는 두 메서드 — 넘겨받은 연결(=그 마이그레이션의 트랜잭션)에서 실행한다."""

    def __init__(self) -> None:
        self.bind = None

    def execute(self, sql) -> None:
        from sqlalchemy import text

        self.bind.execute(text(sql) if isinstance(sql, str) else sql)

    def get_bind(self):
        return self.bind


@pytest.fixture(scope="module")
def m0046():
    op = _Op()
    fake = types.ModuleType("alembic")
    fake.op = op
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        spec = importlib.util.spec_from_file_location("_m0046_under_test", _M0046)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("alembic", None)
        else:
            sys.modules["alembic"] = saved
    assert (module.revision, module.down_revision) == ("0046", "0045")
    module._test_op = op
    return module


async def _run(engine, module, func: str, pid: dict | None = None) -> None:
    """alembic 의 transaction_per_migration 처럼 함수 하나를 트랜잭션 하나로 돌린다(예외면 롤백).
    ``pid`` 를 주면 그 트랜잭션의 backend pid 를 적어 둔다(잠금 대기 관찰용)."""

    def _call(sync_conn) -> None:
        if pid is not None:
            pid["pid"] = sync_conn.exec_driver_sql("SELECT pg_backend_pid()").scalar()
        module._test_op.bind = sync_conn
        getattr(module, func)()

    async with engine.begin() as conn:
        await conn.run_sync(_call)


async def _admin_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, _ = _split_dsn(PROOF_DSN)
    return create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")


def _plain(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture
async def dsn():
    """테스트마다 init SQL 만으로 새 DB — 마이그레이션 함수가 스키마를 바꾸므로 공유하지 않는다."""
    from sqlalchemy import text

    prefix, dbname = _split_dsn(PROOF_DSN)
    owned = f"{dbname}{_OWNED_SUFFIX}"
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp")), owned
    admin = await _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{owned}"'))
    finally:
        await admin.dispose()
    await _apply_init_sql(f"{prefix}/{owned}")
    yield f"{prefix}/{owned}"
    admin = await _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest.fixture
async def engine(dsn):
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(dsn)
    yield eng
    await eng.dispose()


async def _all(engine, sql: str) -> list[tuple]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql))).all()]


async def _state(engine) -> tuple:
    return await _all(engine, _UNIQUE_INDEXES), await _all(engine, _GUARD)


async def _global(engine, alias: str | None, *, rpm=None, tpm=None) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO model.rate_limit_configs "
                "(scope, scope_id, model_alias, rpm_limit, tpm_limit, is_active, created_by) "
                "VALUES ('GLOBAL', NULL, :a, :rpm, :tpm, true, :by)"
            ),
            {"a": alias, "rpm": rpm, "tpm": tpm, "by": SEED_ADMIN_ID},
        )


# ── 1. init SQL = upgrade() ─────────────────────────────────────────────────────────────


async def test_init_sql_and_the_migration_build_the_same_schema(engine, m0046):
    init_only = await _state(engine)
    assert [name for name, _ in init_only[0]] == [
        "model.uq_rate_limit_configs_active",
        "model.uq_rate_limit_configs_global_active",
    ]
    assert [kind for kind, _ in init_only[1]] == ["function", "trigger"]
    assert "DEFERRABLE INITIALLY DEFERRED" in init_only[1][1][1]

    await _run(engine, m0046, "downgrade")  # GLOBAL 행이 없다 → 통과
    indexes, guard = await _state(engine)
    assert [name for name, _ in indexes] == ["model.uq_rate_limit_configs_active"]
    assert guard == [], "downgrade 가 트리거와 함수를 지워야 한다"
    assert indexes != init_only[0][:1], "대조군 — 비교가 정의 차이를 실제로 잡는다"

    await _run(engine, m0046, "upgrade")
    assert await _state(engine) == init_only


# ── 2·3. downgrade 판정 ──────────────────────────────────────────────────────────────────


async def test_downgrade_refuses_while_two_models_hold_a_limit(engine, m0046):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    await _global(engine, "claude-opus-4-7", rpm=60)
    await _global(engine, "claude-sonnet-4-6", tpm=200000)
    await _global(engine, "claude-sonnet-4-6[1m]")  # 비운 행 — 혼자서는 막지 않는다
    await _global(engine, None, rpm=5)  # alias 없는 행 — 집행되지 않으므로 판정에서 빠진다
    before = (await _state(engine), await _all(engine, _ACTIVE_GLOBAL))

    with pytest.raises(RuntimeError) as refused:
        await _run(engine, m0046, "downgrade")
    message = str(refused.value)
    assert "claude-opus-4-7: rpm=60, tpm=None" in message
    assert "claude-sonnet-4-6: rpm=None, tpm=200000" in message
    assert "2 models have one" in message
    assert "claude-sonnet-4-6[1m]" not in message and "None: rpm" not in message
    assert "SET LOCAL rl.allow_global_release = 'on';" in message
    assert "updated_at = now()" in message and "Nothing was changed" in message
    assert textwrap.indent(m0046.KEEP_ONE_GLOBAL_SQL, "      ") in message  # 아래 4 가 실행하는 SQL
    assert "COMMIT;" not in message, "정리 SQL 이 운영자 확인 없이 커밋한다"
    # 트랜잭션이 롤백됐다 — 비활성화도 감사 기록도 남지 않았다.
    assert (await _state(engine), await _all(engine, _ACTIVE_GLOBAL)) == before
    assert await _all(engine, _AUDITED_RELEASES) == []

    # 대조군 — 판정이 없으면 같은 데이터에서 0045 인덱스가 PostgreSQL 의 중복 키 오류로 죽는다.
    with pytest.raises(IntegrityError, match="could not create unique index"):
        async with engine.begin() as conn:
            await conn.execute(text("DROP INDEX model.uq_rate_limit_configs_active"))
            await conn.execute(text(m0046.CREATE_SINGLE_SLOT_INDEX))


async def test_downgrade_releases_unenforced_rows_with_the_bypass_and_audits_them(
    engine, m0046, capsys
):
    from sqlalchemy.exc import IntegrityError

    await _global(engine, "claude-opus-4-7", rpm=60)
    await _global(engine, "claude-sonnet-4-6")  # 비운 행 — 아무것도 집행하지 않는다
    await _global(engine, None, rpm=5)  # alias 없는 행 — 게이트웨이는 GLOBAL 을 alias 로만 읽는다
    # 대조군 — alias 없는 행까지 세던 판정(리뷰 전)에는 실제 한도가 둘이라 이 상태를 거부했다.
    limited = [r for r in await _all(engine, _ACTIVE_GLOBAL) if r[1] is not None or r[2] is not None]
    assert len(limited) == 2

    await _run(engine, m0046, "downgrade")
    printed = capsys.readouterr().out
    assert "deactivated the active GLOBAL row of claude-sonnet-4-6 (rpm=None, tpm=None)" in printed
    assert "deactivated the active GLOBAL row of <no alias> (rpm=5, tpm=None)" in printed
    assert await _all(engine, _ACTIVE_GLOBAL) == [("claude-opus-4-7", 60, None)]
    assert await _all(engine, _AUDITED_RELEASES) == [(None, "5"), ("claude-sonnet-4-6", None)]
    assert (await _state(engine))[1] == [], "트리거·함수가 남았다"

    # 대조군 — 0045 의 단일 슬롯이 돌아왔다: 두 번째 모델의 GLOBAL 행이 거부된다.
    with pytest.raises(IntegrityError, match="uq_rate_limit_configs_active"):
        await _global(engine, "claude-sonnet-4-6", tpm=100000)

    await _run(engine, m0046, "upgrade")
    await _global(engine, "claude-sonnet-4-6", tpm=100000)
    assert await _all(engine, _ACTIVE_GLOBAL) == [
        ("claude-opus-4-7", 60, None),
        ("claude-sonnet-4-6", None, 100000),
    ]
    assert [kind for kind, _ in (await _state(engine))[1]] == ["function", "trigger"]


async def test_control_without_set_constraints_immediate_the_downgrade_ddl_is_refused(engine, m0046):
    """downgrade 가 비활성화한 행의 지연 트리거 이벤트를 먼저 처리하지 않으면, 같은 트랜잭션의
    ``CREATE INDEX`` 를 PostgreSQL 이 거부한다 — ``SET CONSTRAINTS ALL IMMEDIATE`` 가 필요한 이유."""
    from sqlalchemy.exc import DBAPIError

    await _global(engine, "claude-sonnet-4-6")  # 비운 행 — downgrade 가 비활성화한다
    op = m0046._test_op
    real_execute = op.execute
    op.execute = lambda sql: None if sql == "SET CONSTRAINTS ALL IMMEDIATE" else real_execute(sql)
    try:
        with pytest.raises(DBAPIError, match="pending trigger events"):
            await _run(engine, m0046, "downgrade")
    finally:
        op.execute = real_execute
    assert [kind for kind, _ in (await _state(engine))[1]] == ["function", "trigger"]


# ── 4. 정리 SQL(문서·거부 메시지) ───────────────────────────────────────────────────────────


def _documented_cleanup(keep: str) -> str:
    """06-upgrade-rollback.md 의 정리 SQL 블록 — 자리표시자만 채워 **그대로** 실행한다."""
    doc = _ROLLBACK_DOC.read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```sql\n(.*?)```", doc, re.S) if "rl.allow_global_release" in b]
    assert len(blocks) == 1, f"정리 SQL 블록을 하나만 찾아야 한다: {len(blocks)}"
    sql = blocks[0]
    for placeholder in ("<남길 alias>", "<운영자 user id>"):
        assert sql.count(placeholder) == 1, placeholder
    return sql.replace("<남길 alias>", keep).replace("<운영자 user id>", str(SEED_ADMIN_ID))


def _message_cleanup(m0046, keep: str) -> str:
    """downgrade 거부 메시지의 정리 SQL(``KEEP_ONE_GLOBAL_SQL``) — 자리표시자만 채운다."""
    sql = m0046.KEEP_ONE_GLOBAL_SQL
    assert sql.count("<alias to keep>") == 1
    return sql.replace("<alias to keep>", keep)


_SOURCES = ("doc", "message")
_PLACEHOLDER = {"doc": "<남길 alias>", "message": "<alias to keep>"}


def _cleanup(source: str, m0046, keep: str) -> str:
    return _documented_cleanup(keep) if source == "doc" else _message_cleanup(m0046, keep)


def _keep(source: str, keep: str) -> str:
    return _PLACEHOLDER[source] if keep == "placeholder" else keep


# 리뷰 전 형태 그대로(대조군으로만 쓴다) — 남길 alias 를 확인하지 않고 COMMIT 으로 끝났다.
_PREVIOUS_CLEANUP = {
    "doc": """\
BEGIN;
SET LOCAL rl.allow_global_release = 'on';
WITH released AS (
    UPDATE model.rate_limit_configs SET is_active = false, updated_at = now()
     WHERE scope = 'GLOBAL' AND is_active AND model_alias IS DISTINCT FROM '{keep}'
    RETURNING id, model_alias, rpm_limit, tpm_limit
)
INSERT INTO audit.audit_logs
    (actor_user_id, actor_role, action, resource_type, resource_id, changes,
     result, ip_address, request_id)
SELECT '{actor}'::uuid, 'ADMIN', 'DELETE_RATE_LIMIT', 'RateLimitConfig', id::text,
       jsonb_build_object(
           'before', jsonb_build_object(
               'scope', 'GLOBAL', 'model_alias', model_alias,
               'rpm', rpm_limit, 'tpm', tpm_limit),
           'after', jsonb_build_object('scope', 'GLOBAL', 'deactivated', 1),
           'reason', 'manual cleanup before alembic downgrade below 0046'),
       'SUCCESS', '0.0.0.0', 'manual-0046-downgrade'
  FROM released
RETURNING resource_id, changes -> 'before';
COMMIT;
""",
    "message": """\
BEGIN;
SET LOCAL rl.allow_global_release = 'on';
UPDATE model.rate_limit_configs SET is_active = false, updated_at = now()
 WHERE scope = 'GLOBAL' AND is_active
   AND model_alias IS DISTINCT FROM '{keep}';
COMMIT;
""",
}

_TWO_LIMITS = [("claude-opus-4-7", 60, None), ("claude-sonnet-4-6", 30, None)]


async def _two_limits(engine) -> None:
    await _global(engine, "claude-opus-4-7", rpm=60)
    await _global(engine, "claude-sonnet-4-6", rpm=30)
    assert await _all(engine, _ACTIVE_GLOBAL) == _TWO_LIMITS


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.parametrize("keep", ["placeholder", "claude-opus-4.7"])
async def test_the_cleanup_sql_refuses_an_alias_to_keep_without_a_limit(dsn, engine, m0046, source, keep):
    """자리표시자를 그대로 두었거나 alias 에 오타가 있으면 DO 블록이 멈춘다 — 아무것도 해제되지 않고 감사
    기록도 없다. 트랜잭션은 실패 상태로 남아 운영자가 ROLLBACK 한다."""
    import asyncpg

    await _two_limits(engine)
    conn = await asyncpg.connect(_plain(dsn))
    try:
        with pytest.raises(asyncpg.RaiseError, match="has no active GLOBAL limit"):
            await conn.execute(_cleanup(source, m0046, _keep(source, keep)))
        assert conn.is_in_transaction()
        await conn.execute("ROLLBACK")
    finally:
        await conn.close()
    assert await _all(engine, _ACTIVE_GLOBAL) == _TWO_LIMITS
    assert await _all(engine, _AUDITED_RELEASES) == []


@pytest.mark.parametrize("source", _SOURCES)
async def test_the_cleanup_update_releases_nothing_even_if_psql_goes_on_after_the_do_block(
    dsn, engine, m0046, source
):
    """psql 이 오류 뒤에도 트랜잭션을 이어 가면(ON_ERROR_ROLLBACK) DO 블록 다음 문장이 돈다 — 그 경우를 DO 블록을
    뺀 같은 SQL 로 본다. UPDATE 가 같은 조건을 다시 보므로 COMMIT 해도 해제되는 행이 없다."""
    import asyncpg

    await _two_limits(engine)
    sql, removed = re.subn(r"DO \$\$\n.*?\n\s*\$\$;\n", "", _cleanup(source, m0046, "claude-opus-4.7"), flags=re.S)
    assert removed == 1
    conn = await asyncpg.connect(_plain(dsn))
    try:
        await conn.execute(sql)
        await conn.execute("COMMIT")
    finally:
        await conn.close()
    assert await _all(engine, _ACTIVE_GLOBAL) == _TWO_LIMITS
    assert await _all(engine, _AUDITED_RELEASES) == []


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.parametrize("keep", ["placeholder", "claude-opus-4.7"])
async def test_control_the_previous_cleanup_sql_released_every_limit(dsn, engine, source, keep):
    """대조군 — 예전 형태는 자리표시자 그대로·오타 하나로 남길 한도까지 전부 해제하고 커밋했다(오류 없음)."""
    import asyncpg

    await _two_limits(engine)
    sql = _PREVIOUS_CLEANUP[source].format(keep=_keep(source, keep), actor=SEED_ADMIN_ID)
    conn = await asyncpg.connect(_plain(dsn))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()
    assert await _all(engine, _ACTIVE_GLOBAL) == []


@pytest.mark.parametrize("source", _SOURCES)
async def test_the_cleanup_sql_keeps_one_limit_once_the_operator_commits(dsn, engine, m0046, source):
    import asyncpg

    await _two_limits(engine)
    sql = _cleanup(source, m0046, "claude-opus-4-7")

    conn = await asyncpg.connect(_plain(dsn))
    try:
        # 대조군 — 우회 없는 같은 SQL 은 커밋에서 거부되고 아무것도 바뀌지 않는다.
        without_bypass = sql.replace("SET LOCAL rl.allow_global_release = 'on';", "")
        assert without_bypass != sql
        await conn.execute(without_bypass)
        with pytest.raises(asyncpg.CheckViolationError, match="claude-sonnet-4-6"):
            await conn.execute("COMMIT")
        assert await _all(engine, _ACTIVE_GLOBAL) == _TWO_LIMITS

        await conn.execute(sql)  # 그대로 — COMMIT 없이 끝난다
        assert conn.is_in_transaction()
        assert [tuple(r) for r in await conn.fetch(_ACTIVE_GLOBAL)] == [("claude-opus-4-7", 60, None)]
        assert await _all(engine, _ACTIVE_GLOBAL) == _TWO_LIMITS, "COMMIT 전에 다른 연결에 보였다"
        await conn.execute("COMMIT")
        assert await _all(engine, _ACTIVE_GLOBAL) == [("claude-opus-4-7", 60, None)]
        audited = [("claude-sonnet-4-6", "30")] if source == "doc" else []
        assert await _all(engine, _AUDITED_RELEASES) == audited

        # 우회와 남길 alias 는 그 트랜잭션에서 끝났다 — 같은 연결의 다음 해제는 다시 거부된다.
        assert await conn.fetchval("SELECT current_setting('rl.allow_global_release', true)") == ""
        assert await conn.fetchval("SELECT current_setting('rl.keep_alias', true)") == ""
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE model.rate_limit_configs SET is_active = false, updated_at = now() "
                "WHERE scope = 'GLOBAL' AND is_active"
            )
    finally:
        await conn.close()

    await _run(engine, m0046, "downgrade")  # 남은 실제 한도는 하나 — 통과
    assert await _all(engine, _ACTIVE_GLOBAL) == [("claude-opus-4-7", 60, None)]


# ── 5. 잠금 순서 ──────────────────────────────────────────────────────────────────────────

# 리뷰 전 순서 — upgrade 는 SHARE(CREATE INDEX)에서, downgrade 는 SHARE ROW EXCLUSIVE 에서 시작해 DROP INDEX
# 에서 ACCESS EXCLUSIVE 로 올렸다. 대조군으로만 쓴다.
_PREVIOUS_ORDER = {
    "upgrade": [
        "SET LOCAL lock_timeout = '5s'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limit_configs_global_active "
        "ON model.rate_limit_configs (COALESCE(model_alias, '')) "
        "WHERE is_active = true AND scope = 'GLOBAL'",
        "DROP INDEX IF EXISTS model.uq_rate_limit_configs_active",
    ],
    "downgrade": [
        "SET LOCAL lock_timeout = '5s'",
        "LOCK TABLE model.rate_limit_configs IN SHARE ROW EXCLUSIVE MODE",
        "SELECT count(*) FROM model.rate_limit_configs WHERE scope = 'GLOBAL' AND is_active",
        "DROP INDEX IF EXISTS model.uq_rate_limit_configs_global_active",
    ],
}


async def _read_then_write_during(dsn, migrate) -> dict:
    """T 가 테이블을 읽은 채로 ``migrate`` 를 시작시키고, 그것이 잠금을 기다리는 동안 T 가 쓰고 커밋한다.
    그동안 새 읽기 하나가 얼마나 기다리는지도 잰다."""
    import asyncpg

    t, reader, observer = [await asyncpg.connect(_plain(dsn)) for _ in range(3)]
    out: dict = {}
    pid: dict = {}
    try:
        tx = t.transaction()
        await tx.start()
        await t.fetchval("SELECT count(*) FROM model.rate_limit_configs")  # T 가 읽었다

        async def _migrate():
            try:
                await migrate(pid)
                out["migration"] = "ok"
            except Exception as e:  # noqa: BLE001 — 결과로 비교한다
                out["migration"] = type(e).__name__

        task = asyncio.ensure_future(_migrate())
        for _ in range(250):
            if "pid" in pid and await observer.fetchval(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1", pid["pid"]
            ):
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("마이그레이션이 잠금을 기다리지 않았다 — 시나리오가 성립하지 않는다")

        async def _read():
            t0 = time.monotonic()
            await reader.fetchval("SELECT count(*) FROM model.rate_limit_configs")
            out["reader_waited_s"] = time.monotonic() - t0

        read = asyncio.ensure_future(_read())
        await asyncio.sleep(0.1)
        try:
            await t.execute(
                "INSERT INTO model.rate_limit_configs (scope, scope_id, rpm_limit, is_active, created_by) "
                "VALUES ('USER', gen_random_uuid(), 1, true, $1)",
                SEED_ADMIN_ID,
            )
            await asyncio.sleep(0.3)
            await tx.commit()
            out["T"] = "ok"
        except Exception as e:  # noqa: BLE001
            await tx.rollback()
            out["T"] = type(e).__name__
        await asyncio.wait_for(task, 20)
        await asyncio.wait_for(read, 20)
    finally:
        for c in (t, reader, observer):
            await c.close()
    return out


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_a_read_then_write_transaction_survives_0046(dsn, engine, m0046, direction):
    if direction == "upgrade":
        await _run(engine, m0046, "downgrade")  # 0045 상태에서 출발한다

    out = await _read_then_write_during(dsn, lambda pid: _run(engine, m0046, direction, pid))
    assert out["T"] == "ok" and out["migration"] == "ok", out
    # 읽기는 대기 중인 ACCESS EXCLUSIVE 뒤에 줄을 선다 — T 가 끝나고 DDL 이 끝날 때까지(lock_timeout 이 상한).
    assert out["reader_waited_s"] < 5, out


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_control_the_previous_lock_order_deadlocks(dsn, engine, m0046, direction):
    import asyncpg

    if direction == "upgrade":
        await _run(engine, m0046, "downgrade")

    async def _previous(pid: dict) -> None:
        conn = await asyncpg.connect(_plain(dsn))
        try:
            pid["pid"] = conn.get_server_pid()
            async with conn.transaction():
                for statement in _PREVIOUS_ORDER[direction]:
                    await conn.execute(statement)
                raise _Rollback  # 대조군은 스키마를 바꾸지 않는다
        except _Rollback:
            pass
        finally:
            await conn.close()

    out = await _read_then_write_during(dsn, _previous)
    assert "DeadlockDetectedError" in (out["T"], out["migration"]), out


class _Rollback(Exception):
    pass


# ── 6. init SQL 의 0046 블록 ─────────────────────────────────────────────────────────────


def _init_sql_0046_block() -> str:
    """02_create_tables.sql 의 0046 객체 블록(DO $$ … $$;)."""
    src = _INIT_TABLES.read_text(encoding="utf-8")
    match = re.search(r"DO \$\$\nDECLARE\n    prev_lock_timeout text.*?\nEND\n\$\$;", src, re.S)
    assert match and "rl_global_release_guard" in match.group(0), "init SQL 의 0046 블록을 못 찾았다"
    return match.group(0)


async def _open_writer(dsn):
    """rate_limit_configs 에 쓰고 커밋하지 않은 트랜잭션 — ROW EXCLUSIVE 를 쥐고 있다."""
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    tx = conn.transaction()
    await tx.start()
    await conn.execute(
        "INSERT INTO model.rate_limit_configs (scope, scope_id, rpm_limit, is_active, created_by) "
        "VALUES ('USER', gen_random_uuid(), 1, true, $1)",
        SEED_ADMIN_ID,
    )
    return conn, tx


async def test_init_sql_block_takes_no_lock_when_the_objects_exist(dsn):
    import asyncpg

    writer, tx = await _open_writer(dsn)
    conn = await asyncpg.connect(_plain(dsn))
    try:
        t0 = time.monotonic()
        await conn.execute(_init_sql_0046_block())
        assert time.monotonic() - t0 < 1, "객체가 있는데 잠금을 기다렸다"
        assert await conn.fetchval("SHOW lock_timeout") == "0", "블록 밖으로 lock_timeout 이 샜다"

        # 대조군 — 맨 CREATE UNIQUE INDEX IF NOT EXISTS 는 인덱스가 있어도 SHARE 를 기다린다.
        await conn.execute("SET lock_timeout = '1s'")
        with pytest.raises(asyncpg.LockNotAvailableError):
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limit_configs_global_active "
                "ON model.rate_limit_configs (COALESCE(model_alias, '')) "
                "WHERE is_active = true AND scope = 'GLOBAL'"
            )
    finally:
        await conn.close()
        await tx.rollback()
        await writer.close()


async def test_init_sql_block_gives_up_after_lock_timeout_and_can_run_again(dsn):
    """0045 DB(0046 객체 없음)에서 열린 쓰기 뒤에 막히면 5초 뒤 깨끗이 실패한다 — 아무것도 만들지 않고,
    쓰기가 끝난 뒤 다시 돌리면 인덱스를 만든다(마이그레이션 Job 재실행과 같다). 트리거는 0046 이 만든다(7)."""
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    try:
        await conn.execute("DROP TRIGGER rl_global_release_guard ON model.rate_limit_configs")
        await conn.execute("DROP INDEX model.uq_rate_limit_configs_global_active")
        await _stamp(conn, "0045")
        writer, tx = await _open_writer(dsn)
        try:
            t0 = time.monotonic()
            with pytest.raises(asyncpg.LockNotAvailableError):
                await conn.execute(_init_sql_0046_block())
            waited = time.monotonic() - t0
        finally:
            await tx.commit()
            await writer.close()
        assert 4 < waited < 10, waited
        assert await conn.fetchval("SELECT to_regclass('model.uq_rate_limit_configs_global_active')") is None

        await conn.execute(_init_sql_0046_block())
        assert await conn.fetchval("SELECT to_regclass('model.uq_rate_limit_configs_global_active')")
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'rl_global_release_guard'"
        ) == 0
    finally:
        await conn.close()


async def test_rerunning_the_whole_init_sql_does_not_wait_behind_a_rate_limit_writer(dsn):
    """02_create_tables.sql 을 통째로 다시 돌려도(매 마이그레이션 Job) rate_limit_configs 의 열린 쓰기 뒤에서
    기다리지 않는다 — 그 테이블의 인덱스·트리거 문장은 전부 존재를 먼저 보는 블록 안에 있다."""
    import asyncpg

    writer, tx = await _open_writer(dsn)
    conn = await asyncpg.connect(_plain(dsn))
    try:
        await conn.execute("SET lock_timeout = '1s'")  # 1초라도 기다리면 실패한다
        t0 = time.monotonic()
        await conn.execute(_INIT_TABLES.read_text(encoding="utf-8"))
        assert time.monotonic() - t0 < 5

        # 대조군 — 블록에 넣기 전의 맨 문장은 인덱스가 있어도 SHARE 를 기다린다.
        with pytest.raises(asyncpg.LockNotAvailableError):
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rate_limit_configs_scope "
                "ON model.rate_limit_configs (scope, scope_id) WHERE is_active = true"
            )
    finally:
        await conn.close()
        await tx.rollback()
        await writer.close()


# ── 7. init SQL 이 트리거를 만드는 DB ────────────────────────────────────────────────────────


async def _stamp(conn, version: str | None, schema: str = "public") -> None:
    """그 스키마의 alembic_version 을 그 리비전으로 만든다(None = 테이블 없음 — 새 설치)."""
    await conn.execute(f'DROP TABLE IF EXISTS "{schema}".alembic_version')
    if version is not None:
        await conn.execute(
            f'CREATE TABLE "{schema}".alembic_version (version_num VARCHAR(32) PRIMARY KEY)'
        )
        await conn.execute(f'INSERT INTO "{schema}".alembic_version VALUES ($1)', version)


async def _with_a_role_named_schema(conn) -> str:
    """접속한 롤과 같은 이름의 스키마를 만들고 search_path 를 PostgreSQL 기본값(``"$user", public``)으로 둔다.
    그 롤로 도는 alembic(version_table_schema 없음)은 alembic_version 을 이 스키마에 만든다."""
    role = await conn.fetchval("SELECT current_user")
    await conn.execute(f'CREATE SCHEMA "{role}"')
    await conn.execute('SET search_path = "$user", public')
    return role


@pytest.mark.parametrize(
    "version, guard_installed",
    [(None, True), ("0046", True), ("0047", True), ("0045", False), ("0042", False),
     ("0023", False), ("not-a-revision", False)],
)
async def test_init_sql_installs_the_guard_only_on_a_fresh_install_or_at_0046(
    dsn, version, guard_installed
):
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    try:
        await conn.execute("DROP TRIGGER IF EXISTS rl_global_release_guard ON model.rate_limit_configs")
        await _stamp(conn, version)
        await conn.execute(_init_sql_0046_block())
        installed = await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'rl_global_release_guard'"
        )
        assert installed == (1 if guard_installed else 0), (version, installed)
    finally:
        await conn.close()


async def _as_a_0042_db(conn, schema: str = "public") -> None:
    """init SQL 로 만든 DB 를 0042 의 모양으로 — 트리거·GLOBAL 인덱스 없음, 0024 의 단일 슬롯 인덱스,
    alembic_version 0042(이 차트의 Job 이 0046 에 닿기 전에 멈춘 DB 가 이렇다)."""
    await conn.execute("DROP TRIGGER rl_global_release_guard ON model.rate_limit_configs")
    await conn.execute("DROP INDEX model.uq_rate_limit_configs_global_active")
    await conn.execute("DROP INDEX model.uq_rate_limit_configs_active")
    await conn.execute(_executed_0024_index_sql("uq_rate_limit_configs_active"))
    await _stamp(conn, "0042", schema)


_MAIN_SLOT_SWAP = (  # main(이전 릴리스)의 GLOBAL upsert — alias 와 무관하게 활성 GLOBAL 행을 전부 비활성화
    "UPDATE model.rate_limit_configs SET is_active = false, updated_at = now() "
    "WHERE scope = 'GLOBAL' AND scope_id IS NULL AND is_active",
    "INSERT INTO model.rate_limit_configs (scope, model_alias, rpm_limit, is_active, created_by) "
    "VALUES ('GLOBAL', 'claude-sonnet-4-6', 30, true, $1)",
)


async def _main_slot_swap(conn) -> None:
    async with conn.transaction():
        await conn.execute(_MAIN_SLOT_SWAP[0])
        await conn.execute(_MAIN_SLOT_SWAP[1], SEED_ADMIN_ID)


async def test_a_0042_db_whose_upgrade_stops_before_0046_keeps_main_slot_swaps(dsn, engine):
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    try:
        await _as_a_0042_db(conn)
        await _global(engine, "claude-opus-4-7", rpm=60)
    finally:
        await conn.close()

    await _apply_init_sql(dsn)  # 이 차트의 Job 첫 단계 — 그 뒤 alembic 이 0046 전에 실패했다고 본다
    indexes, guard = await _state(engine)
    assert [kind for kind, _ in guard] == ["function"], "트리거가 0046 전에 생겼다"
    single_slot = dict(indexes)["model.uq_rate_limit_configs_active"]
    assert "scope <>" not in single_slot, "init SQL 이 0024 의 단일 슬롯 정의를 바꿨다"

    conn = await asyncpg.connect(_plain(dsn))
    try:
        await _main_slot_swap(conn)  # 이전 admin-api 가 GLOBAL 을 다른 모델로 옮긴다 — 0042 에서는 정상이다
    finally:
        await conn.close()
    assert await _all(engine, _ACTIVE_GLOBAL) == [("claude-sonnet-4-6", 30, None)]


async def test_control_with_the_guard_on_a_0042_db_main_slot_swaps_are_refused(dsn, engine):
    """대조군 — 이전 init SQL 처럼 트리거를 무조건 만들면, 같은 0042 DB 에서 이전 admin-api 의 정상적인 슬롯
    교체가 커밋에서 거부된다."""
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    try:
        await _as_a_0042_db(conn)
        await _global(engine, "claude-opus-4-7", rpm=60)
        await conn.execute(_release_guard_trigger_ddl())
        with pytest.raises(asyncpg.CheckViolationError, match="claude-opus-4-7"):
            await _main_slot_swap(conn)
    finally:
        await conn.close()
    assert await _all(engine, _ACTIVE_GLOBAL) == [("claude-opus-4-7", 60, None)]


@pytest.mark.parametrize(
    "where, version, guard_installed",
    [("role", "0042", False), ("role", None, True), ("public", "0045", False)],
)
async def test_init_sql_reads_alembic_version_where_alembic_writes_it(dsn, where, version, guard_installed):
    """롤 이름의 스키마가 있으면 alembic 은 alembic_version 을 그쪽에 둔다. init SQL 도 같은 롤의 search_path
    로 찾는다 — 그 0042 DB 는 새 설치가 아니고, 그 스키마가 비어 있으면 public 을 본다."""
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    try:
        role = await _with_a_role_named_schema(conn)
        await conn.execute("DROP TRIGGER IF EXISTS rl_global_release_guard ON model.rate_limit_configs")
        await _stamp(conn, None)
        await _stamp(conn, version, role if where == "role" else "public")
        await conn.execute(_init_sql_0046_block())
        installed = await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'rl_global_release_guard'"
        )
        assert installed == (1 if guard_installed else 0), (where, version, installed)
    finally:
        await conn.close()


def _public_qualified(block: str) -> str:
    """예전 판정 — alembic_version 을 public 으로 못박았다. 대조군으로만 쓴다."""
    out = block.replace("to_regclass('alembic_version')", "to_regclass('public.alembic_version')")
    out = out.replace("FROM alembic_version ", "FROM public.alembic_version ")
    assert out.count("public.alembic_version") == 2, "판정 문장을 못 찾았다"
    return out


async def test_a_role_schema_0042_db_keeps_main_slot_swaps(dsn, engine):
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    try:
        role = await _with_a_role_named_schema(conn)
        await _as_a_0042_db(conn, role)
        await _global(engine, "claude-opus-4-7", rpm=60)
        await conn.execute(_init_sql_0046_block())
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'rl_global_release_guard'"
        ) == 0
        await _main_slot_swap(conn)
    finally:
        await conn.close()
    assert await _all(engine, _ACTIVE_GLOBAL) == [("claude-sonnet-4-6", 30, None)]


async def test_control_a_public_qualified_lookup_takes_a_role_schema_0042_db_for_a_fresh_install(dsn, engine):
    """대조군 — public 으로 못박은 판정은 롤 이름 스키마의 0042 DB 에 트리거를 만들고, 이전 admin-api 의 정상적인
    슬롯 교체가 23514(check_violation)로 거부된다."""
    import asyncpg

    conn = await asyncpg.connect(_plain(dsn))
    try:
        role = await _with_a_role_named_schema(conn)
        await _as_a_0042_db(conn, role)
        await _global(engine, "claude-opus-4-7", rpm=60)
        await conn.execute(_public_qualified(_init_sql_0046_block()))
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'rl_global_release_guard'"
        ) == 1
        with pytest.raises(asyncpg.CheckViolationError, match="claude-opus-4-7") as refused:
            await _main_slot_swap(conn)
        assert refused.value.sqlstate == "23514"
    finally:
        await conn.close()
    assert await _all(engine, _ACTIVE_GLOBAL) == [("claude-opus-4-7", 60, None)]


def _release_guard_trigger_ddl() -> str:
    src = _INIT_TABLES.read_text(encoding="utf-8")
    match = re.search(r"CREATE CONSTRAINT TRIGGER rl_global_release_guard\b.*?;", src, re.S)
    assert match, "rl_global_release_guard trigger DDL not found in init SQL"
    return match.group(0)


# ── 8. 이름을 그대로 쓰는 이유 ────────────────────────────────────────────────────────────


async def test_the_previous_init_sql_cannot_bring_the_single_slot_back(engine):
    """롤백한 차트의 Job 이 0046 DB 에서 이전 init SQL 을 다시 돌려도(그 Job 은 뒤이은 alembic 에서
    ``Can't locate revision`` 으로 실패하지만 init SQL 은 이미 커밋된 뒤다) 모델별 GLOBAL 은 그대로다."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    previous = _executed_0024_index_sql("uq_rate_limit_configs_active")  # 이전 init SQL 과 같은 정의
    await _global(engine, "claude-opus-4-7", rpm=60)
    async with engine.begin() as conn:
        await conn.execute(text(previous))  # 이름이 있으므로 no-op
    await _global(engine, "claude-sonnet-4-6", tpm=100000)
    assert len(await _all(engine, _ACTIVE_GLOBAL)) == 2

    # 대조군 — 그 이름이 없었다면(새 이름을 붙였다면) 같은 문장이 단일 슬롯을 되살린다.
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL rl.allow_global_release = 'on'"))  # 의도된 해제
        await conn.execute(text(
            "UPDATE model.rate_limit_configs SET is_active = false, updated_at = now() "
            "WHERE scope = 'GLOBAL' AND model_alias = 'claude-sonnet-4-6'"
        ))
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX model.uq_rate_limit_configs_active"))
        await conn.execute(text(previous))
    with pytest.raises(IntegrityError, match="uq_rate_limit_configs_active"):
        await _global(engine, "claude-sonnet-4-6", tpm=100000)
