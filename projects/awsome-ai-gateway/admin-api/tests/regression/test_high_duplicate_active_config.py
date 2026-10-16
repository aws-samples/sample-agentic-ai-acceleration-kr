# Copyright 2026 © Amazon.com and Affiliates
"""Regression: #52 / #53 — versioned config upserts must not leave two is_active=true rows.

Bug: ``BudgetRepository.upsert_config`` and ``RateLimitConfigRepository.upsert`` deactivate the
current active row and then insert a new one. Two overlapping calls for the same key (admin
double-click, ALB retry, two uvicorn workers) each ran their deactivate-UPDATE against a snapshot
predating the other's commit, then both inserted. The read paths use ``scalar_one_or_none()``, so
from that point on every read of that scope raised ``MultipleResultsFound`` → HTTP 500 — and it
never healed, because nothing in the product removes the extra row.

Fix, two independent layers:
  1. ``pg_advisory_xact_lock`` on the logical key in the write path (``app/repositories/_locks.py``),
     so the second caller *succeeds* — it waits, then correctly supersedes — rather than merely
     failing safely;
  2. partial UNIQUE indexes (migration ``0024``, and ``db/init/02_create_tables.sql`` for fresh
     installs) as the database-level backstop for any writer that skips the lock.

Both regressions share one fix, one migration and one test rig, so they share one file.

Since migration ``0046`` GLOBAL rate limits are per model: one active GLOBAL row per model alias
(``uq_rate_limit_configs_global_active``), while USER/TEAM keep one active row per subject
(``uq_rate_limit_configs_active``, now excluding GLOBAL). ``TestGlobalIndexIsPerModel`` and
``TestGlobalSavesArePerModel`` race saves of different and of identical aliases on that schema.
0046 also adds ``rl_global_release_guard``, a deferred constraint trigger that refuses to commit the
release of a GLOBAL row whose model keeps no active row. ``TestThePreviousReleasesGlobalSaveIsRefused``
drives the previous release's alias-blind upsert against it, alone and racing this release's.

Two findings were measured on real PostgreSQL 16 while developing this fix and are asserted below,
because each is easy to get wrong in a way that still looks correct:

  * ``SELECT ... FOR UPDATE`` is NOT sufficient. It serializes only when a row already exists; on
    the first write for a key there is nothing to lock, so both transactions insert. Row locks
    cannot provide mutual exclusion over a key that does not exist yet — and "set a budget for a
    team that has none" is the common case.
  * The unique indexes MUST wrap the nullable key columns in ``COALESCE``. NULLs are distinct in a
    unique index, so a plain index leaves exactly the rows that matter unprotected: the org-wide
    budget (``client IS NULL``) and the GLOBAL rate limit (``scope_id IS NULL``). Since 0046 the
    GLOBAL index is keyed on ``COALESCE(model_alias, '')`` for the same reason.

The static assertions run everywhere. The behavioural proofs need real PostgreSQL — MVCC snapshot
semantics are the thing under test, so a mock or SQLite would prove nothing — and are skipped
unless ``PROOF_DSN`` is set::

    docker run -d --name pg-proof -p 55432:5432 -e POSTGRES_PASSWORD=proof \\
        -e POSTGRES_DB=gwproof postgres:16-alpine
    PROOF_DSN=postgresql+asyncpg://postgres:proof@127.0.0.1:55432/gwproof \\
        pytest tests/regression/test_high_duplicate_active_config.py

⚠️ These fixtures ``DROP SCHEMA auth/budget/model CASCADE`` and rebuild only the four tables this
file needs. That is destructive to *any* other DB test sharing the same ``PROOF_DSN``, because the
rebuild does not restore the rest of the schema. Measured before this was fixed: running the suite
left ``model`` holding just ``model_aliases`` and ``rate_limit_configs`` — ``model_pricings``,
``routing_profiles``, ``team_allowed_models`` and ``user_allowed_models`` were gone, and
``test_high_model_registry_claude5.py`` failed with ``relation "model.routing_profiles" does not
exist``. Reversing the two files' order turned ``2 failed`` back into ``45 passed``, which is the
proof that order, not either file's logic, was the variable. This file always passed either way —
the damage only ever showed up in whatever ran next, so it could never self-detect.

Fix: the fixtures do not touch ``PROOF_DSN``'s database at all. They create their own
``<dbname>_dupcfg`` database, drop and rebuild schemas *inside* it, and drop it on teardown.
Isolation is structural — the destructive DDL can no longer reach a database anything else reads.
Schema-prefix isolation was ruled out because the ORM hardcodes ``__table_args__ = {"schema":
"budget"}`` / ``{"schema": "model"}``, so the repositories under test would not follow a rename.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.repositories import _locks
from app.repositories._locks import advisory_xact_lock
from app.repositories.budget_repository import BudgetRepository
from app.repositories.model_repository import RateLimitConfigRepository

NIL_UUID = "00000000-0000-0000-0000-000000000000"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION = _REPO_ROOT / "db" / "versions" / "0024_unique_active_config_indexes.py"
_INIT_SQL = _REPO_ROOT / "db" / "init" / "02_create_tables.sql"

PROOF_DSN = os.environ.get("PROOF_DSN")
_DB = pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN not set (needs a real PostgreSQL)")


# ===========================================================================
# Layer 1 — the write path must take the advisory lock, keyed correctly.
# ===========================================================================

@pytest.mark.unit
def test_budget_upsert_locks_before_deactivating():
    src = inspect.getsource(BudgetRepository.upsert_config)
    assert "advisory_xact_lock" in src, (
        "upsert_config must serialize on the logical key; without it two concurrent writes each "
        "insert an active row and every later read of that scope returns 500"
    )
    assert src.index("advisory_xact_lock") < src.index("update(BudgetConfig)"), (
        "the lock must be taken BEFORE the deactivate-UPDATE, or the race window stays open"
    )


@pytest.mark.unit
def test_budget_lock_key_includes_client():
    """The lock key must match the UPDATE's predicate exactly. ``client`` is in that predicate
    (per-app budgets are separate active rows), so it must be in the key: omitting it would
    serialize unrelated per-app writes, and the key would no longer identify one active row."""
    src = inspect.getsource(BudgetRepository.upsert_config)
    lock_call = src[src.index("advisory_xact_lock"):src.index("update(BudgetConfig)")]
    for part in ("config.scope", "config.scope_id", "config.client"):
        assert part in lock_call, f"budget lock key must include {part}"


@pytest.mark.unit
def test_rate_limit_upsert_locks_on_a_key_without_model_alias():
    """For USER/TEAM ``RateLimitConfigRepository.upsert`` is deliberately alias-blind — it
    deactivates every active row of the subject regardless of model_alias — so the lock key must
    mirror that: adding model_alias would let two concurrent writes for different aliases race and
    duplicate. GLOBAL deactivates per alias since 0046 but keeps this coarser key on purpose: it
    serializes the previous release's alias-blind GLOBAL upsert (same key) with this one during a
    rolling deploy (see the comment in ``upsert``)."""
    src = inspect.getsource(RateLimitConfigRepository.upsert)
    assert "advisory_xact_lock" in src
    lock_call = src[src.index("advisory_xact_lock"):src.index("update(RateLimitConfig)")]
    assert "config.scope" in lock_call and "config.scope_id" in lock_call
    assert "model_alias" not in lock_call, (
        "the rate-limit lock key must not include model_alias — the upsert predicate does not"
    )


@pytest.mark.unit
def test_lock_is_transaction_scoped():
    """A session-scoped ``pg_advisory_lock`` would survive the transaction and leak into whichever
    request next borrows that pooled connection (and would pin it under RDS Proxy)."""
    assert "pg_advisory_xact_lock" in _locks._ADVISORY_LOCK_SQL.text
    assert not re.search(r"\bpg_advisory_lock\s*\(", _locks._ADVISORY_LOCK_SQL.text)


class _RecordingSession:
    """Captures the bound lock key so key derivation can be tested without a database."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def execute(self, statement, params=None):  # noqa: ANN001 - test double
        self.keys.append(params["lock_key"])
        return None


async def _key(*parts: object) -> str:
    session = _RecordingSession()
    await advisory_xact_lock(session, *parts)  # type: ignore[arg-type]
    return session.keys[0]


@pytest.mark.unit
async def test_lock_key_distinguishes_every_part():
    """Two different logical keys must never collapse onto the same lock, or writes to unrelated
    scopes would block each other; and the same logical key must always produce the same lock."""
    team_a, team_b = uuid.uuid4(), uuid.uuid4()
    assert await _key("budget_configs", "TEAM", team_a, None) == \
           await _key("budget_configs", "TEAM", team_a, None)
    assert await _key("budget_configs", "TEAM", team_a, None) != \
           await _key("budget_configs", "TEAM", team_b, None)
    assert await _key("budget_configs", "TEAM", team_a, None) != \
           await _key("budget_configs", "TEAM", team_a, "claude-code")
    assert await _key("budget_configs", "TEAM", team_a) != \
           await _key("rate_limit_configs", "TEAM", team_a)
    # Order-sensitive: a naive key that summed or set-ified the parts would collide here.
    assert await _key("a", "b") != await _key("b", "a")


@pytest.mark.unit
async def test_lock_key_treats_none_as_absent():
    """``None`` renders as the empty string. Harmless for the two callers: ``client`` is
    constrained to NULL or one of the app names (ck_budget_configs_client), so '' never occurs,
    and ``scope_id`` is a UUID."""
    assert await _key("t", None) == await _key("t", "")


# ===========================================================================
# Layer 2 — the schema backstop. Both DDL sources must agree.
# ===========================================================================

@pytest.mark.unit
def test_migration_0024_is_on_the_chain():
    assert _MIGRATION.exists(), "migration 0024 is missing"
    src = _MIGRATION.read_text()
    assert 'revision = "0024"' in src
    assert 'down_revision = "0023"' in src, "a wrong down_revision branches the chain"


@pytest.mark.unit
def test_migration_0024_dedupes_before_creating_the_indexes():
    """CREATE UNIQUE INDEX fails outright if duplicates already exist, which would abort the
    upgrade on exactly the installations that need it most. The dedupe must come first, and it
    must demote rather than delete so the history stays auditable."""
    src = _MIGRATION.read_text()
    upgrade = src.split("def upgrade")[1]
    assert upgrade.index("DEDUPE_BUDGET_CONFIGS") < upgrade.index("CREATE UNIQUE INDEX")
    assert upgrade.index("DEDUPE_RATE_LIMIT_CONFIGS") < upgrade.index("CREATE UNIQUE INDEX")
    assert "row_number() OVER" in src, "dedupe must keep exactly one row per key"
    assert "SET is_active = false" in src, "losers must be demoted, not deleted"


def _index_ddl(name: str) -> str:
    """The shipped CREATE statement for one index, taken from db/init/02_create_tables.sql.

    The behavioural proofs below run against this exact text rather than a copy, so the rig cannot
    drift away from what customers actually install.
    """
    src = _INIT_SQL.read_text()
    match = re.search(rf"CREATE UNIQUE INDEX IF NOT EXISTS {name}\b.*?;", src, re.DOTALL)
    assert match, f"{name} not found in {_INIT_SQL.name}"
    return match.group(0).rstrip(";")


def _migration_index_ddl(name: str) -> str:
    """The SQL migration 0024 *emits* for one index.

    Asserting against the raw file text would be wrong: the migration builds the rate-limit index
    with an f-string, so the sentinel UUID never appears literally in the source. Resolve
    ``{NIL_UUID}`` from the module's own constant — which also pins the sentinel to the nil UUID,
    the one value ``gen_random_uuid()`` (v4) can never produce.
    """
    src = _MIGRATION.read_text()
    sentinel = re.search(r'^NIL_UUID = "([0-9a-f-]+)"$', src, re.MULTILINE)
    assert sentinel, "migration 0024 must define NIL_UUID"
    assert sentinel.group(1) == NIL_UUID, (
        f"sentinel drifted to {sentinel.group(1)!r}; a non-nil UUID could collide with a real "
        f"scope_id and wrongly reject a legitimate row"
    )
    # Each index is one op.execute( ... ) call whose closing paren sits at 4-space indent.
    match = re.search(rf"CREATE UNIQUE INDEX IF NOT EXISTS {name}\b.*?\n    \)", src, re.DOTALL)
    assert match, f"{name} not created in {_MIGRATION.name}"
    return match.group(0).replace("{NIL_UUID}", NIL_UUID)


def _release_guard_ddl() -> list[str]:
    """0046's ``rl_global_release_guard`` function and constraint trigger, taken from db/init (the
    trigger statement is lifted out of the guarded DO block so it runs on its own)."""
    src = _INIT_SQL.read_text()
    function = re.search(
        r"CREATE OR REPLACE FUNCTION model\.rl_global_release_guard\(\).*?\n\$\$;", src, re.DOTALL
    )
    trigger = re.search(r"CREATE CONSTRAINT TRIGGER rl_global_release_guard\b.*?;", src, re.DOTALL)
    assert function and trigger, "rl_global_release_guard not found in db/init/02_create_tables.sql"
    return [function.group(0).rstrip(";"), trigger.group(0).rstrip(";")]


def _executed_0024_index_sql(name: str) -> str:
    """The exact statement 0024's ``upgrade()`` executes for one index (module constants resolved) —
    executable, unlike ``_migration_index_ddl``'s source fragment."""
    from tests.regression.test_high_model_registry_claude5 import _op_execute_sql

    prefix = f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
    hits = [s for s in _op_execute_sql(_MIGRATION, "upgrade").split("\n") if s.startswith(prefix)]
    assert len(hits) == 1, f"{name}: {hits}"
    return hits[0]


def _all_index_ddl(name: str) -> list[tuple[str, str]]:
    """(source label, emitted DDL) for both places the index is defined."""
    return [(_INIT_SQL.name, _index_ddl(name)), (_MIGRATION.name, _migration_index_ddl(name))]


@pytest.mark.unit
def test_both_ddl_sources_define_both_indexes():
    """Fresh installs run db/init; upgrades run migration 0024. A guard that only checked one
    would let the other rot."""
    for name in ("uq_budget_configs_active", "uq_rate_limit_configs_active"):
        for label, ddl in _all_index_ddl(name):
            assert name in ddl, f"{label}: {name} missing"


@pytest.mark.unit
def test_indexes_coalesce_the_nullable_key_columns():
    """The NULL trap, in both DDL sources. NULLs are distinct in a unique index, so without
    COALESCE the org-wide budget row (client IS NULL) and the GLOBAL rate limit (scope_id IS NULL)
    — the two highest blast-radius rows — are not protected at all."""
    for label, ddl in _all_index_ddl("uq_budget_configs_active"):
        assert "COALESCE(client, '')" in ddl, f"{label}: client must be COALESCEd"
    for label, ddl in _all_index_ddl("uq_rate_limit_configs_active"):
        assert f"COALESCE(scope_id, '{NIL_UUID}'::uuid)" in ddl, (
            f"{label}: scope_id must be COALESCEd — GLOBAL scope stores NULL"
        )
    assert "(COALESCE(model_alias, ''))" in _index_ddl("uq_rate_limit_configs_global_active"), (
        "the GLOBAL index must COALESCE model_alias, or two alias-less GLOBAL rows coexist"
    )


@pytest.mark.unit
def test_dedupe_partitions_match_the_index_keys():
    """The dedupe must group by exactly what the index makes unique. A mismatch would either
    leave duplicates behind (index creation then fails, aborting the upgrade) or demote rows the
    index would have allowed."""
    src = _MIGRATION.read_text().replace("{NIL_UUID}", NIL_UUID)
    assert "PARTITION BY scope, scope_id, COALESCE(client, '')" in src
    assert f"PARTITION BY scope, COALESCE(scope_id, '{NIL_UUID}'::uuid)" in src


@pytest.mark.unit
def test_rate_limit_index_is_not_keyed_on_model_alias():
    """Mirrors the lock key. Since the USER/TEAM upsert deactivates alias-blind, keying that index
    on model_alias would leave the real duplicate window open while looking protective. (GLOBAL has
    its own index, keyed on the alias — 0046.)"""
    for label, ddl in _all_index_ddl("uq_rate_limit_configs_active"):
        assert "model_alias" not in ddl, f"{label}: {ddl}"


@pytest.mark.unit
def test_indexes_are_partial_on_is_active():
    """The indexes must be partial. A full unique index would reject the *second* update to any
    budget, because the deactivated history rows share the key."""
    for name in ("uq_budget_configs_active", "uq_rate_limit_configs_active"):
        for label, ddl in _all_index_ddl(name):
            assert "WHERE is_active = true" in ddl, f"{label}: {name} must be partial"
    assert "WHERE is_active = true AND scope = 'GLOBAL'" in _index_ddl(
        "uq_rate_limit_configs_global_active"
    )


@pytest.mark.unit
def test_the_two_rate_limit_indexes_split_the_scopes_without_overlap():
    """0046: the USER/TEAM index must exclude GLOBAL — or, keyed on (scope, nil) for every GLOBAL
    row, it would keep GLOBAL single-slot no matter what the GLOBAL index says — and the GLOBAL
    index must cover only GLOBAL."""
    assert _index_ddl("uq_rate_limit_configs_active").endswith(
        "WHERE is_active = true AND scope <> 'GLOBAL'"
    )
    assert "scope = 'GLOBAL'" in _index_ddl("uq_rate_limit_configs_global_active")


# ===========================================================================
# Behavioural proofs — real PostgreSQL, real concurrent transactions.
# ===========================================================================

_ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

# Minimal slice of the asset schema: the two tables under test plus the FK targets they need.
# Enum members and the client CHECK mirror db/init/02_create_tables.sql; the unique indexes are
# read from that file verbatim by _index_ddl().
_BASE_DDL = [
    "DROP SCHEMA IF EXISTS budget CASCADE",
    "DROP SCHEMA IF EXISTS model CASCADE",
    "DROP SCHEMA IF EXISTS auth CASCADE",
    "CREATE SCHEMA auth",
    "CREATE SCHEMA budget",
    "CREATE SCHEMA model",
    "CREATE TABLE auth.users (id UUID PRIMARY KEY)",
    "CREATE TYPE budget.budget_scope  AS ENUM ('TEAM', 'USER')",
    "CREATE TYPE budget.period_type   AS ENUM ('MONTHLY')",
    "CREATE TYPE budget.budget_policy AS ENUM ('HARD_BLOCK', 'SOFT_WARNING', 'THROTTLE')",
    # 0037 이 도메인으로 alert_thresholds 원소를 1~100 으로 제약한다. 이 스크래치 DDL 이
    # 도메인·컬럼을 빠뜨리면 BudgetConfig INSERT 가 실 PG 에서 UndefinedColumn 으로 깨진다.
    "CREATE DOMAIN budget.alert_pct AS INTEGER CHECK (VALUE BETWEEN 1 AND 100)",
    "CREATE TYPE model.rate_limit_scope AS ENUM ('USER', 'TEAM', 'GLOBAL')",
    """
    CREATE TABLE budget.budget_configs (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        scope           budget.budget_scope  NOT NULL,
        scope_id        UUID                 NOT NULL,
        client          VARCHAR(32)          NULL,
        max_budget_usd  NUMERIC(12,4)        NOT NULL,
        period_type     budget.period_type   NOT NULL DEFAULT 'MONTHLY',
        policy          budget.budget_policy NOT NULL DEFAULT 'HARD_BLOCK',
        allocated_by    UUID                 NOT NULL REFERENCES auth.users(id),
        effective_from  DATE                 NOT NULL,
        is_active       BOOLEAN              NOT NULL DEFAULT true,
        created_at      TIMESTAMPTZ          NOT NULL DEFAULT now(),
        alert_thresholds budget.alert_pct[]  NOT NULL DEFAULT '{80,90,100}',
        CONSTRAINT ck_budget_configs_client
            CHECK (client IS NULL OR client IN ('claude-code','cowork','codex'))
    )
    """,
    "CREATE TABLE model.model_aliases (alias VARCHAR(128) PRIMARY KEY)",
    "INSERT INTO model.model_aliases (alias) VALUES ('alias-a'), ('alias-b'), ('alias-c')",
    """
    CREATE TABLE model.rate_limit_configs (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        scope         model.rate_limit_scope NOT NULL,
        scope_id      UUID,
        model_alias   VARCHAR(128) REFERENCES model.model_aliases(alias),
        rpm_limit     INTEGER,
        tpm_limit     INTEGER,
        cpm_limit_usd NUMERIC(10,4),
        cph_limit_usd NUMERIC(10,4),
        is_active     BOOLEAN      NOT NULL DEFAULT true,
        created_by    UUID         NOT NULL REFERENCES auth.users(id),
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
]


def _require_scratch_db(dsn: str) -> None:
    """Refuse to run against anything that is not obviously a throwaway database.

    Even though the destructive DDL now lands in a private ``_dupcfg`` database, this guard stays:
    creating and dropping a sibling database next to a production one is still not something to do
    by accident, and the name is the only signal available before connecting.
    """
    dbname = dsn.rsplit("/", 1)[-1].split("?")[0].lower()
    if not any(token in dbname for token in ("proof", "test", "scratch", "tmp")):
        pytest.fail(
            f"PROOF_DSN points at database {dbname!r}. These tests create a sibling "
            f"'<dbname>_dupcfg' database and DROP the auth/budget/model schemas inside it, so "
            f"they only run against a scratch database whose name contains "
            f"'proof', 'test', 'scratch' or 'tmp'."
        )


# The private database these fixtures own outright. Suffixed rather than randomised so a crashed
# run leaves one predictable leftover to drop, not an unbounded pile of them; the DROP below is
# unconditional at setup, so a leftover from a previous crash is cleaned up rather than inherited.
_OWNED_DB_SUFFIX = "_dupcfg"


def _split_dsn(dsn: str) -> tuple[str, str]:
    """('postgresql+asyncpg://u:p@h:5432', 'gwproof') — prefix and database name."""
    prefix, dbname = dsn.rsplit("/", 1)
    return prefix, dbname.split("?")[0]


async def _recreate_owned_db() -> str:
    """Drop and create this file's private database. Returns its DSN.

    CREATE/DROP DATABASE cannot run inside a transaction block, hence AUTOCOMMIT. The connection
    is made to ``postgres`` rather than to PROOF_DSN's database because you cannot drop the
    database you are connected to.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    prefix, dbname = _split_dsn(PROOF_DSN)
    owned = f"{dbname}{_OWNED_DB_SUFFIX}"
    admin = create_async_engine(f"{prefix}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            # WITH (FORCE) so a leaked connection from a crashed run cannot block the drop —
            # PostgreSQL 13+. Without it the failure mode is "database is being accessed by other
            # users", which historically got misread as a transient and skipped past, leaving a
            # half-built database behind.
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
                text(f'DROP DATABASE IF EXISTS "{dbname}{_OWNED_DB_SUFFIX}" WITH (FORCE)')
            )
    finally:
        await admin.dispose()


async def _bootstrap(
    *, with_indexes: bool, with_guard: bool | None = None, extra_ddl: tuple[str, ...] = ()
):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models.auth  # noqa: F401 — registers auth.users so the FK targets resolve

    _require_scratch_db(PROOF_DSN)
    ddl = list(_BASE_DDL)
    if with_indexes:
        ddl += [
            _index_ddl("uq_budget_configs_active"),
            _index_ddl("uq_rate_limit_configs_active"),
            _index_ddl("uq_rate_limit_configs_global_active"),
        ]
    if with_indexes if with_guard is None else with_guard:
        ddl += _release_guard_ddl()
    ddl += list(extra_ddl)

    # gen_random_uuid() lives in pgcrypto on PostgreSQL < 13 and is built in from 13 on; the
    # shipped DDL relies on it, so make it available before the tables that default to it.
    engine = create_async_engine(await _recreate_owned_db())
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        for statement in ddl:
            await conn.execute(text(statement))
        await conn.execute(text("INSERT INTO auth.users (id) VALUES (:i)"), {"i": _ACTOR})
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def sm():
    """Schema as shipped: advisory lock in the code, unique indexes and the GLOBAL release guard in
    the database."""
    engine, maker = await _bootstrap(with_indexes=True)
    yield maker
    await engine.dispose()
    await _drop_owned_db()


@pytest.fixture
async def sm_no_index():
    """Indexes omitted, so the lock is the only defense — this is what isolates layer 1 and lets
    the original bug still be reproduced by the adversary repos below."""
    engine, maker = await _bootstrap(with_indexes=False)
    yield maker
    await engine.dispose()
    await _drop_owned_db()


@pytest.fixture
async def sm_no_guard():
    """The shipped indexes without 0046's release guard — the control for the guard proofs."""
    engine, maker = await _bootstrap(with_indexes=True, with_guard=False)
    yield maker
    await engine.dispose()
    await _drop_owned_db()


@pytest.fixture
async def sm_single_slot():
    """Revision 0045's rate-limit index (0024's DDL, one active GLOBAL row in total) — the control
    for the per-model GLOBAL proofs."""
    engine, maker = await _bootstrap(
        with_indexes=False, extra_ddl=(_executed_0024_index_sql("uq_rate_limit_configs_active"),)
    )
    yield maker
    await engine.dispose()
    await _drop_owned_db()


def _budget(scope_id: uuid.UUID, amount: str, client: str | None = None):
    from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope, PeriodType

    return BudgetConfig(
        id=uuid.uuid4(), scope=BudgetScope.TEAM, scope_id=scope_id, client=client,
        max_budget_usd=Decimal(amount), period_type=PeriodType.MONTHLY,
        policy=BudgetPolicy.HARD_BLOCK, allocated_by=_ACTOR,
        effective_from=date.today(), is_active=True,
    )


def _rate_limit(scope, scope_id: uuid.UUID | None, rpm: int | None, model_alias: str | None = None):
    from app.models.model import RateLimitConfig

    return RateLimitConfig(
        id=uuid.uuid4(), scope=scope, scope_id=scope_id, model_alias=model_alias,
        rpm_limit=rpm, is_active=True, created_by=_ACTOR,
    )


# --- Adversaries. Deliberate copies of the pre-fix write paths: with the advisory lock in place
# --- nothing races, so without these the index layer would never be exercised and a weakened
# --- index (one that lost its COALESCE, say) would pass unnoticed.

class _LockFreeBudgetRepo(BudgetRepository):
    async def upsert_config(self, config):
        from sqlalchemy import update

        from app.models.budget import BudgetConfig

        stmt = update(BudgetConfig).where(
            BudgetConfig.scope == config.scope,
            BudgetConfig.scope_id == config.scope_id,
            BudgetConfig.is_active.is_(True),
        )
        if config.client is not None:
            stmt = stmt.where(BudgetConfig.client == config.client)
        else:
            stmt = stmt.where(BudgetConfig.client.is_(None))
        await self._session.execute(stmt.values(is_active=False))
        self._session.add(config)
        await self._session.flush()
        return config


class _ForUpdateBudgetRepo(_LockFreeBudgetRepo):
    """The tempting alternative: lock the rows we are about to deactivate."""

    async def upsert_config(self, config):
        from sqlalchemy import select

        from app.models.budget import BudgetConfig

        await self._session.execute(
            select(BudgetConfig.id)
            .where(
                BudgetConfig.scope == config.scope,
                BudgetConfig.scope_id == config.scope_id,
                BudgetConfig.is_active.is_(True),
            )
            .with_for_update()
        )
        return await super().upsert_config(config)


class _LockFreeRateLimitRepo(RateLimitConfigRepository):
    async def upsert(self, config):
        from sqlalchemy import update

        from app.models.model import RateLimitConfig

        await self._session.execute(
            update(RateLimitConfig)
            .where(
                RateLimitConfig.scope == config.scope,
                RateLimitConfig.scope_id == config.scope_id,
                RateLimitConfig.is_active.is_(True),
            )
            .values(is_active=False)
        )
        self._session.add(config)
        await self._session.flush()
        return config


class _SingleSlotRateLimitRepo(RateLimitConfigRepository):
    """The GLOBAL upsert as it was before 0046 (same lock, alias-blind deactivate) — the control
    for the per-model proofs. With it a GLOBAL save releases every other model's limit."""

    async def upsert(self, config):
        from sqlalchemy import update

        from app.models.model import RateLimitConfig

        await advisory_xact_lock(
            self._session, "rate_limit_configs", config.scope.value, config.scope_id
        )
        await self._session.execute(
            update(RateLimitConfig)
            .where(
                RateLimitConfig.scope == config.scope,
                RateLimitConfig.scope_id == config.scope_id,
                RateLimitConfig.is_active.is_(True),
            )
            .values(is_active=False)
        )
        self._session.add(config)
        await self._session.flush()
        return config


async def _budget_upsert(session, cfg, repo=BudgetRepository):
    return await repo(session).upsert_config(cfg)


async def _rl_upsert(session, cfg, repo=RateLimitConfigRepository):
    return await repo(session).upsert(cfg)


async def _race(sm, upsert, cfg_a, cfg_b, *, repo=None) -> list[BaseException]:
    """Two genuinely overlapping transactions: tx1 does its writes, tx2 starts while tx1 is still
    open, and only then does tx1 commit. Returns whatever exceptions were raised."""
    from sqlalchemy import text

    kwargs = {} if repo is None else {"repo": repo}

    async def tx(cfg, started=None, gate=None):
        async with sm() as session:
            # Fail loudly instead of hanging the suite if a lock is genuinely stuck.
            await session.execute(text("SET lock_timeout = '10s'"))
            await upsert(session, cfg, **kwargs)
            if started is not None:
                started.set()
            if gate is not None:
                await gate.wait()
            await session.commit()

    started, gate = asyncio.Event(), asyncio.Event()
    t1 = asyncio.create_task(tx(cfg_a, started, gate))
    await asyncio.wait_for(started.wait(), 10)
    t2 = asyncio.create_task(tx(cfg_b))
    await asyncio.sleep(0.5)
    gate.set()
    results = await asyncio.wait_for(asyncio.gather(t1, t2, return_exceptions=True), 40)
    return [r for r in results if isinstance(r, BaseException)]


async def _scalar(sm, sql: str, **params):
    from sqlalchemy import text

    async with sm() as session:
        return (await session.execute(text(sql), params)).scalar_one()


_ACTIVE_BUDGETS = (
    "SELECT count(*) FROM budget.budget_configs "
    "WHERE scope='TEAM' AND scope_id=:t AND is_active=true"
)
_ACTIVE_LIMITS_FOR_TEAM = (
    "SELECT count(*) FROM model.rate_limit_configs "
    "WHERE scope='TEAM' AND scope_id=:t AND is_active=true"
)
_ACTIVE_GLOBAL_LIMITS = (
    "SELECT count(*) FROM model.rate_limit_configs "
    "WHERE scope='GLOBAL' AND scope_id IS NULL AND is_active=true"
)


@_DB
@pytest.mark.integration
class TestTheBugIsRealAndForUpdateDoesNotFixIt:
    """Reproduce #52/#53 with the lock removed, on a schema without the backstop indexes. If these
    ever stop reproducing, the rig has broken and every test below is vacuous."""

    async def test_unlocked_upserts_leave_two_active_rows(self, sm_no_index):
        team = uuid.uuid4()
        errs = await _race(sm_no_index, _budget_upsert, _budget(team, "100"), _budget(team, "200"),
                           repo=_LockFreeBudgetRepo)
        assert not errs, f"the original bug was silent, not an error: {errs}"
        assert await _scalar(sm_no_index, _ACTIVE_BUDGETS, t=team) == 2

    async def test_duplicate_rows_break_the_read_path_permanently(self, sm_no_index):
        """The user-visible symptom, and why "it heals itself" was never an option: the corrupted
        scope keeps raising until someone edits the table by hand."""
        from sqlalchemy.exc import MultipleResultsFound

        from app.models.budget import BudgetScope

        team = uuid.uuid4()
        await _race(sm_no_index, _budget_upsert, _budget(team, "100"), _budget(team, "200"),
                    repo=_LockFreeBudgetRepo)
        async with sm_no_index() as session:
            with pytest.raises(MultipleResultsFound):
                await BudgetRepository(session).get_active_config(BudgetScope.TEAM, team)

    async def test_select_for_update_does_not_serialize_the_first_write(self, sm_no_index):
        """DISPROOF of the obvious fix, measured on PostgreSQL 16. With no existing row there is
        nothing to lock, so FOR UPDATE locks nothing and both transactions insert. This is why the
        shipped fix locks the logical key instead."""
        team = uuid.uuid4()
        errs = await _race(sm_no_index, _budget_upsert, _budget(team, "100"), _budget(team, "200"),
                           repo=_ForUpdateBudgetRepo)
        assert not errs, f"unexpected errors: {errs}"
        assert await _scalar(sm_no_index, _ACTIVE_BUDGETS, t=team) == 2, (
            "FOR UPDATE unexpectedly serialized a first write — re-derive the fix before relying "
            "on the advisory-lock rationale"
        )

    async def test_select_for_update_does_serialize_when_a_row_exists(self, sm_no_index):
        """The other half of the measurement, recorded so the disproof above is not overstated:
        once a row exists, FOR UPDATE is sufficient. tx2 blocks in the SELECT; its subsequent
        UPDATE is a new statement whose fresh READ COMMITTED snapshot does see tx1's inserted row.
        The first-write case is decisive on its own."""
        team = uuid.uuid4()
        async with sm_no_index() as session:
            await _budget_upsert(session, _budget(team, "50"), repo=_ForUpdateBudgetRepo)
            await session.commit()
        errs = await _race(sm_no_index, _budget_upsert, _budget(team, "100"), _budget(team, "200"),
                           repo=_ForUpdateBudgetRepo)
        assert not errs, f"unexpected errors: {errs}"
        assert await _scalar(sm_no_index, _ACTIVE_BUDGETS, t=team) == 1

    async def test_unlocked_global_rate_limit_upserts_duplicate(self, sm_no_index):
        """#53, on the row that applies to every user."""
        from app.models.model import RateLimitScope

        errs = await _race(sm_no_index, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000),
                           repo=_LockFreeRateLimitRepo)
        assert not errs, f"{errs}"
        assert await _scalar(sm_no_index, _ACTIVE_GLOBAL_LIMITS) == 2


@_DB
@pytest.mark.integration
class TestTheAdvisoryLockFixesIt:
    """Layer 1 alone, indexes absent: concurrent writes must now SUCCEED and converge."""

    async def test_budget_first_write_for_a_new_team(self, sm_no_index):
        team = uuid.uuid4()
        errs = await _race(sm_no_index, _budget_upsert, _budget(team, "100"), _budget(team, "200"))
        assert not errs, f"concurrent budget writes must both succeed: {errs}"
        assert await _scalar(sm_no_index, _ACTIVE_BUDGETS, t=team) == 1

    async def test_budget_update_of_an_existing_row(self, sm_no_index):
        team = uuid.uuid4()
        async with sm_no_index() as session:
            await _budget_upsert(session, _budget(team, "50"))
            await session.commit()
        errs = await _race(sm_no_index, _budget_upsert, _budget(team, "100"), _budget(team, "200"))
        assert not errs, f"{errs}"
        assert await _scalar(sm_no_index, _ACTIVE_BUDGETS, t=team) == 1

    async def test_the_last_committed_write_wins(self, sm_no_index):
        """Serializing must also yield the right value. tx2 blocks on the lock, so it commits last
        and its budget is the one in force — otherwise we would have traded a 500 for silent data
        loss, which is worse."""
        team = uuid.uuid4()
        await _race(sm_no_index, _budget_upsert, _budget(team, "100"), _budget(team, "200"))
        assert await _scalar(
            sm_no_index,
            "SELECT max_budget_usd FROM budget.budget_configs "
            "WHERE scope='TEAM' AND scope_id=:t AND is_active=true",
            t=team,
        ) == Decimal("200.0000")

    async def test_budget_reads_stay_healthy(self, sm_no_index):
        from app.models.budget import BudgetScope

        team = uuid.uuid4()
        await _race(sm_no_index, _budget_upsert, _budget(team, "100"), _budget(team, "200"))
        async with sm_no_index() as session:
            cfg = await BudgetRepository(session).get_active_config(BudgetScope.TEAM, team)
        assert cfg is not None and cfg.max_budget_usd == Decimal("200.0000")

    async def test_rate_limit_team_scope(self, sm_no_index):
        from app.models.model import RateLimitScope

        team = uuid.uuid4()
        errs = await _race(sm_no_index, _rl_upsert,
                           _rate_limit(RateLimitScope.TEAM, team, 60),
                           _rate_limit(RateLimitScope.TEAM, team, 120))
        assert not errs, f"{errs}"
        assert await _scalar(sm_no_index, _ACTIVE_LIMITS_FOR_TEAM, t=team) == 1

    async def test_rate_limit_global_scope_with_null_scope_id(self, sm_no_index):
        """GLOBAL is stored as scope_id=NULL (rate_limit_service, roi_aggregator), so the lock key
        must survive a None part — and this is the highest blast-radius row in the table."""
        from app.models.model import RateLimitScope

        errs = await _race(sm_no_index, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000))
        assert not errs, f"{errs}"
        assert await _scalar(sm_no_index, _ACTIVE_GLOBAL_LIMITS) == 1

    async def test_rate_limit_reads_stay_healthy(self, sm_no_index):
        from app.models.model import RateLimitScope

        team = uuid.uuid4()
        await _race(sm_no_index, _rl_upsert,
                    _rate_limit(RateLimitScope.TEAM, team, 60),
                    _rate_limit(RateLimitScope.TEAM, team, 120))
        async with sm_no_index() as session:
            cfg = await RateLimitConfigRepository(session).get_active(RateLimitScope.TEAM, team)
        assert cfg is not None and cfg.rpm_limit == 120

    async def test_two_aliases_racing_on_one_scope_still_converge(self, sm_no_index):
        """The behavioural reason the lock key omits model_alias. The upsert deactivates every
        active row for the scope regardless of alias, so two concurrent writes carrying *different*
        aliases are still writes to the same logical row. Keyed on alias they would take different
        locks, race, and duplicate — which the static key guard catches, but this is the failure it
        is guarding against."""
        from app.models.model import RateLimitScope

        team = uuid.uuid4()
        errs = await _race(sm_no_index, _rl_upsert,
                           _rate_limit(RateLimitScope.TEAM, team, 60, "alias-a"),
                           _rate_limit(RateLimitScope.TEAM, team, 120, "alias-b"))
        assert not errs, f"{errs}"
        assert await _scalar(sm_no_index, _ACTIVE_LIMITS_FOR_TEAM, t=team) == 1


@_DB
@pytest.mark.integration
class TestTheIndexIsAWorkingBackstop:
    """Layer 2, exercised by the adversary repos: a writer that skips the lock must be stopped by
    the database rather than corrupting the table."""

    async def test_index_rejects_an_unlocked_budget_writer(self, sm):
        team = uuid.uuid4()
        errs = await _race(sm, _budget_upsert, _budget(team, "100"), _budget(team, "200"),
                           repo=_LockFreeBudgetRepo)
        assert len(errs) == 1, f"expected the unlocked loser to be rejected, got {errs}"
        assert await _scalar(sm, _ACTIVE_BUDGETS, t=team) == 1

    async def test_index_rejects_an_unlocked_writer_of_the_null_client_row(self, sm):
        """COALESCE(client, '') is what makes the index bite here; a plain 3-column index would let
        two NULL-client rows coexist because NULL != NULL."""
        team = uuid.uuid4()
        errs = await _race(sm, _budget_upsert,
                           _budget(team, "100", None), _budget(team, "200", None),
                           repo=_LockFreeBudgetRepo)
        assert len(errs) == 1, f"COALESCE(client,'') must treat two NULLs as equal, got {errs}"
        assert await _scalar(sm, _ACTIVE_BUDGETS, t=team) == 1

    async def test_index_rejects_an_unlocked_writer_of_the_global_rate_limit(self, sm):
        """The scope_id NULL trap. Without COALESCE(scope_id, nil) this lands two active GLOBAL
        rows and every rate-limit read returns 500."""
        from app.models.model import RateLimitScope

        errs = await _race(sm, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000),
                           repo=_LockFreeRateLimitRepo)
        assert len(errs) == 1, f"COALESCE(scope_id, nil) must treat two NULLs as equal, got {errs}"
        assert await _scalar(sm, _ACTIVE_GLOBAL_LIMITS) == 1

    async def test_index_does_not_fire_on_the_serialized_path(self, sm):
        """The shipped combination. If the index fired here, the fix would have turned a rare 500
        into a common one."""
        team = uuid.uuid4()
        errs = await _race(sm, _budget_upsert, _budget(team, "100"), _budget(team, "200"))
        assert not errs, f"index must not fire when the lock already serialized: {errs}"
        assert await _scalar(sm, _ACTIVE_BUDGETS, t=team) == 1


@_DB
@pytest.mark.integration
class TestLegitimateMultiRowCasesStillWork:
    """Non-regression: the fix must not narrow what the product legitimately stores."""

    async def test_different_teams_do_not_block_each_other(self, sm):
        """The lock is per-key. If it were global — or keyed too coarsely — every concurrent admin
        write would serialize, so team B must complete while team A's transaction is still open."""
        from sqlalchemy import text

        team_a, team_b = uuid.uuid4(), uuid.uuid4()
        started, gate = asyncio.Event(), asyncio.Event()

        async def hold_a():
            async with sm() as session:
                await _budget_upsert(session, _budget(team_a, "100"))
                started.set()
                await gate.wait()
                await session.commit()

        t1 = asyncio.create_task(hold_a())
        await asyncio.wait_for(started.wait(), 10)
        async with sm() as session:
            await session.execute(text("SET lock_timeout = '3s'"))
            await _budget_upsert(session, _budget(team_b, "300"))
            await session.commit()          # must not wait on team A
        gate.set()
        await asyncio.wait_for(t1, 10)
        assert await _scalar(sm, _ACTIVE_BUDGETS, t=team_a) == 1
        assert await _scalar(sm, _ACTIVE_BUDGETS, t=team_b) == 1

    async def test_per_app_budgets_coexist_with_the_total_budget(self, sm):
        """The total row (client IS NULL) and one row per app are all active at once by design."""
        team = uuid.uuid4()
        for client in (None, "claude-code", "cowork", "codex"):
            async with sm() as session:
                await _budget_upsert(session, _budget(team, "10", client))
                await session.commit()
        assert await _scalar(sm, _ACTIVE_BUDGETS, t=team) == 4

    async def test_global_team_and_user_rate_limits_coexist(self, sm):
        from app.models.model import RateLimitScope

        team, user = uuid.uuid4(), uuid.uuid4()
        for scope, scope_id in (
            (RateLimitScope.GLOBAL, None),
            (RateLimitScope.TEAM, team),
            (RateLimitScope.USER, user),
        ):
            async with sm() as session:
                await _rl_upsert(session, _rate_limit(scope, scope_id, 10))
                await session.commit()
        assert await _scalar(
            sm, "SELECT count(*) FROM model.rate_limit_configs WHERE is_active=true"
        ) == 3

    async def test_repeated_updates_keep_history(self, sm):
        """Partial index, so history rows accumulate freely while exactly one stays active."""
        team = uuid.uuid4()
        for amount in ("10", "20", "30", "40"):
            async with sm() as session:
                await _budget_upsert(session, _budget(team, amount))
                await session.commit()
        assert await _scalar(
            sm, "SELECT count(*) FROM budget.budget_configs WHERE scope_id=:t", t=team
        ) == 4
        assert await _scalar(sm, _ACTIVE_BUDGETS, t=team) == 1


_ACTIVE_GLOBAL_BY_ALIAS = (
    "SELECT model_alias, rpm_limit FROM model.rate_limit_configs "
    "WHERE scope='GLOBAL' AND scope_id IS NULL AND is_active=true ORDER BY model_alias"
)


async def _rows(sm, sql: str):
    from sqlalchemy import text

    async with sm() as session:
        return [tuple(r) for r in (await session.execute(text(sql))).all()]


@_DB
@pytest.mark.integration
class TestGlobalIndexIsPerModel:
    """0046 — the schema keeps one active GLOBAL row per model alias. Exercised by the lock-free
    adversary, so it is the index alone that is under test: it must reject a second active row for
    the same model and admit rows for different models."""

    async def test_index_rejects_an_unlocked_writer_of_the_same_alias(self, sm):
        from app.models.model import RateLimitScope

        errs = await _race(sm, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-a"),
                           repo=_LockFreeRateLimitRepo)
        assert len(errs) == 1, f"expected the unlocked loser to be rejected, got {errs}"
        assert "uq_rate_limit_configs_global_active" in str(errs[0])
        assert await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", 1000)]

    async def test_control_without_the_index_the_unlocked_writers_duplicate(self, sm_no_index):
        from app.models.model import RateLimitScope

        errs = await _race(sm_no_index, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-a"),
                           repo=_LockFreeRateLimitRepo)
        assert not errs, f"{errs}"
        assert await _rows(sm_no_index, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", 1000), ("alias-a", 2000)]

    async def test_index_admits_unlocked_writers_of_different_aliases(self, sm):
        """The GLOBAL index must not over-constrain: different models are different keys."""
        from app.models.model import RateLimitScope

        errs = await _race(sm, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-b"),
                           repo=_LockFreeRateLimitRepo)
        assert not errs, f"{errs}"
        assert await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", 1000), ("alias-b", 2000)]

    async def test_control_the_0045_index_rejects_the_second_alias(self, sm_single_slot):
        """Revision 0045's index is why per-model GLOBAL limits needed a migration, not just code."""
        from app.models.model import RateLimitScope

        errs = await _race(sm_single_slot, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-b"),
                           repo=_LockFreeRateLimitRepo)
        assert len(errs) == 1 and "uq_rate_limit_configs_active" in str(errs[0]), f"{errs}"
        assert await _rows(sm_single_slot, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", 1000)]


@_DB
@pytest.mark.integration
class TestGlobalSavesArePerModel:
    """The shipped write path (advisory lock + per-alias GLOBAL upsert) on the 0046 schema: saving one
    model never releases another, and two saves of the same model end with exactly one active row."""

    async def test_two_aliases_saved_concurrently_both_stay_active(self, sm):
        from app.models.model import RateLimitScope

        errs = await _race(sm, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-b"))
        assert not errs, f"{errs}"
        assert await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", 1000), ("alias-b", 2000)]

    async def test_control_the_single_slot_upsert_releases_the_other_alias(self, sm_no_guard):
        """The same race through the pre-0046 upsert keeps only the later save — what the test above
        would see with the old code, on a database without the release guard."""
        from app.models.model import RateLimitScope

        errs = await _race(sm_no_guard, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-b"),
                           repo=_SingleSlotRateLimitRepo)
        assert not errs, f"{errs}"
        assert await _rows(sm_no_guard, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-b", 2000)]

    async def test_same_alias_saved_concurrently_leaves_one_active_row(self, sm):
        """The lock serializes them: no error, the later save is the one in force. (Against a writer
        that skips the lock it is the index that guarantees it — TestGlobalIndexIsPerModel.)"""
        from app.models.model import RateLimitScope

        errs = await _race(sm, _rl_upsert,
                           _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                           _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-a"))
        assert not errs, f"{errs}"
        assert await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", 2000)]

    async def test_clearing_one_alias_leaves_the_other(self, sm):
        """Clearing = saving that model with no limits (the console's 'empty and save'). Only that
        model's row changes."""
        from app.models.model import RateLimitScope

        for cfg in (_rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                    _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-b"),
                    _rate_limit(RateLimitScope.GLOBAL, None, None, "alias-a")):
            async with sm() as session:
                await _rl_upsert(session, cfg)
                await session.commit()
        assert await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", None), ("alias-b", 2000)]

    async def test_a_new_model_is_saved_while_other_models_hold_limits(self, sm):
        from app.models.model import RateLimitScope

        await _save_all(sm, _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                        _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-b"),
                        _rate_limit(RateLimitScope.GLOBAL, None, 3000, "alias-c"))
        assert await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS) == [
            ("alias-a", 1000), ("alias-b", 2000), ("alias-c", 3000),
        ]


async def _save_all(sm, *cfgs, repo=RateLimitConfigRepository) -> None:
    for cfg in cfgs:
        async with sm() as session:
            await _rl_upsert(session, cfg, repo=repo)
            await session.commit()


async def _race_pair(sm, first, second) -> list[BaseException | None]:
    """Like ``_race`` but each transaction has its own (config, repository). ``first`` takes the
    shared advisory lock and holds its transaction open until ``second`` is observed **waiting**
    for that lock (pg_stat_activity), so the interleaving does not depend on a sleep. Returns each
    transaction's exception, or None, in order."""
    from sqlalchemy import text

    second_pid: dict = {}

    async def tx(cfg, repo, started=None, gate=None, pid=None):
        async with sm() as session:
            await session.execute(text("SET lock_timeout = '10s'"))
            if pid is not None:
                pid["pid"] = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await _rl_upsert(session, cfg, repo=repo)
            if started is not None:
                started.set()
            if gate is not None:
                await gate.wait()
            await session.commit()

    started, gate = asyncio.Event(), asyncio.Event()
    t1 = asyncio.create_task(tx(*first, started, gate))
    await asyncio.wait_for(started.wait(), 10)
    t2 = asyncio.create_task(tx(*second, pid=second_pid))
    async with sm() as observer:
        for _ in range(500):
            if "pid" in second_pid and (await observer.execute(text(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :p"
            ), {"p": second_pid["pid"]})).scalar():
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("the second transaction never waited for the advisory lock")
    gate.set()
    results = await asyncio.wait_for(asyncio.gather(t1, t2, return_exceptions=True), 40)
    return [r if isinstance(r, BaseException) else None for r in results]


def _refused_by_the_release_guard(exc: BaseException | None) -> bool:
    """check_violation (23514) raised by the 0046 trigger at commit — asyncpg carries the
    constraint name on the original error, which SQLAlchemy chains as the cause."""
    from sqlalchemy.exc import IntegrityError

    if not isinstance(exc, IntegrityError):
        return False
    cause = getattr(exc.orig, "__cause__", None)
    return (
        getattr(exc.orig, "sqlstate", None) == "23514"
        and getattr(cause, "constraint_name", None) == "rl_global_release_guard"
    )


@_DB
@pytest.mark.integration
class TestThePreviousReleasesGlobalSaveIsRefused:
    """0046 DB + the previous admin-api (``_SingleSlotRateLimitRepo``: the same lock, an alias-blind
    deactivate). That happens during the roll and after an image-only ``helm rollback``. Its GLOBAL
    save would release every other model's GLOBAL limit. The deferred release guard refuses it at
    commit, so nothing changes. This release's saves still pass, because they replace the same
    model's row in the same transaction."""

    @pytest.mark.parametrize("others", [("alias-b", "alias-c"), ("alias-b",)])
    async def test_a_main_style_save_that_releases_others_is_refused_at_commit(self, sm, others):
        from app.models.model import RateLimitScope

        await _save_all(sm, *(_rate_limit(RateLimitScope.GLOBAL, None, 2000, a) for a in others))
        before = await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS)

        with pytest.raises(Exception) as refused:
            await _save_all(sm, _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                            repo=_SingleSlotRateLimitRepo)
        assert _refused_by_the_release_guard(refused.value), refused.value
        assert await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS) == before, "nothing may change"

    async def test_control_without_the_guard_the_main_style_save_releases_others(self, sm_no_guard):
        from app.models.model import RateLimitScope

        await _save_all(sm_no_guard, _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-b"),
                        _rate_limit(RateLimitScope.GLOBAL, None, 3000, "alias-c"))
        await _save_all(sm_no_guard, _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                        repo=_SingleSlotRateLimitRepo)
        assert await _rows(sm_no_guard, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", 1000)]

    async def test_a_main_style_save_of_the_only_model_still_works(self, sm):
        """With no other model's GLOBAL row there is nothing to release — the previous release can
        still change its one GLOBAL limit."""
        from app.models.model import RateLimitScope

        await _save_all(sm, _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-a"))
        await _save_all(sm, _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                        repo=_SingleSlotRateLimitRepo)
        assert await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", 1000)]

    @pytest.mark.parametrize("main_first", [True, False], ids=["main-first", "head-first"])
    async def test_a_mixed_main_and_head_race_releases_nothing(self, sm, main_first):
        """The rolling-deploy race, in both orders: a previous-release pod saves alias-a while a pod
        of this release saves alias-b. They serialize on the shared lock key. The main-style save
        is refused (it would release alias-b), and the per-model save goes through."""
        from app.models.model import RateLimitScope

        await _save_all(sm, _rate_limit(RateLimitScope.GLOBAL, None, 1000, "alias-a"),
                        _rate_limit(RateLimitScope.GLOBAL, None, 2000, "alias-b"))
        main = (_rate_limit(RateLimitScope.GLOBAL, None, 1500, "alias-a"), _SingleSlotRateLimitRepo)
        head = (_rate_limit(RateLimitScope.GLOBAL, None, 2500, "alias-b"), RateLimitConfigRepository)

        first, second = (main, head) if main_first else (head, main)
        errs = await _race_pair(sm, first, second)
        main_err, head_err = (errs[0], errs[1]) if main_first else (errs[1], errs[0])
        assert _refused_by_the_release_guard(main_err), errs
        assert head_err is None, errs
        assert await _rows(sm, _ACTIVE_GLOBAL_BY_ALIAS) == [("alias-a", 1000), ("alias-b", 2500)]

