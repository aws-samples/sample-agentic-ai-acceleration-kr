# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Transaction-scoped advisory locks for deactivate-then-insert upserts.

Why this exists
---------------
``budget_configs`` / ``rate_limit_configs`` are versioned tables: an upsert sets the previous
row's ``is_active=false`` and inserts a new active row. Two concurrent upserts for the same
logical key (an admin double-click, an ALB retry, two uvicorn workers) both run their
deactivate-UPDATE against a snapshot that predates the other's commit, then both INSERT — leaving
**two** ``is_active=true`` rows. Every subsequent read uses ``scalar_one_or_none()``, so it raises
``MultipleResultsFound`` → HTTP 500, permanently, for that scope.

Why not ``SELECT ... FOR UPDATE``
---------------------------------
Measured on PostgreSQL 16: a row lock is *sufficient only when a row already exists*. On the first
write for a key there is no row to lock, so both transactions proceed and duplicate. Row locks
cannot provide mutual exclusion over a key that does not exist yet — and "set a budget for a team
that has none" is the common case.

``pg_advisory_xact_lock`` locks the **logical key** instead, so it works whether or not a row
exists. Transaction scope matters: the lock is released on COMMIT/ROLLBACK, leaving no session
state behind, which keeps it safe under connection pooling and RDS Proxy (a session-scoped
``pg_advisory_lock`` would leak into whoever reuses the connection).

A partial UNIQUE index (see migration ``0024``) remains the backstop for any writer that forgets
to take the lock.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# md5 -> bit(64) -> bigint. Deliberately not hashtext(): that function is internal and its hash
# is not guaranteed stable across major PostgreSQL versions.
_ADVISORY_LOCK_SQL = text("SELECT pg_advisory_xact_lock(('x' || md5(:lock_key))::bit(64)::bigint)")


async def advisory_xact_lock(session: AsyncSession, *parts: object) -> None:
    """Serialize the current transaction against others sharing the same logical key.

    Blocks until the holder commits or rolls back; released automatically at transaction end.
    ``parts`` are joined into the key, so callers must include every column that distinguishes
    one active row from another (e.g. scope, scope_id, client) — otherwise unrelated writes
    serialize against each other and admin throughput suffers.
    """
    key = ":".join("" if p is None else str(p) for p in parts)
    await session.execute(_ADVISORY_LOCK_SQL, {"lock_key": key})
