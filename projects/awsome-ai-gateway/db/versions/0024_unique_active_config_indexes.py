# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""partial UNIQUE indexes on budget_configs / rate_limit_configs active rows

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-11

Both tables are versioned: an upsert sets the previous row's is_active=false and inserts a new
active row. Concurrent upserts of the same key (admin double-click, ALB retry, two uvicorn
workers) could each deactivate against a stale snapshot and then insert, leaving TWO
is_active=true rows. Reads use scalar_one_or_none(), so from that moment every read of that scope
raises MultipleResultsFound -> HTTP 500, permanently. The corruption is silent until the next read.

The write paths now take a transaction-scoped advisory lock (app/repositories/_locks.py); these
indexes are the database-level backstop for any writer that does not.

NULL handling is the subtle part -- NULLs are distinct in a unique index, so an index over a
nullable column does not constrain NULL rows at all:
  * budget_configs.client   IS NULL for the org-wide budget  -> COALESCE(client, '')
  * rate_limit_configs.scope_id IS NULL for GLOBAL scope     -> COALESCE(scope_id, nil UUID)
Without the COALESCE, the exact rows that matter most (the org budget, the global rate limit)
would remain unprotected. The nil UUID is safe as a sentinel because gen_random_uuid() (v4)
cannot produce it.

rate_limit_configs is keyed WITHOUT model_alias on purpose: RateLimitConfigRepository.upsert()
is alias-blind (it deactivates every active row for the scope), so at most one active row per
scope is intended. Including model_alias would leave the real duplicate window open.

Pre-existing duplicates: CREATE UNIQUE INDEX fails outright if any exist, which would abort the
migration and block deployment. Deployed installations may already have them, so this migration
first collapses each duplicate group to its newest row (the value an operator last submitted and
therefore expects to see) and logs what it touched.
"""
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

NIL_UUID = "00000000-0000-0000-0000-000000000000"

# Keep only the newest row per logical key; older duplicates are demoted, not deleted, so the
# history stays auditable and the change is reversible by inspection.
DEDUPE_BUDGET_CONFIGS = """
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY scope, scope_id, COALESCE(client, '')
               ORDER BY created_at DESC, id DESC
           ) AS rn
    FROM budget.budget_configs
    WHERE is_active = true
)
UPDATE budget.budget_configs c
   SET is_active = false
  FROM ranked r
 WHERE c.id = r.id AND r.rn > 1
"""

DEDUPE_RATE_LIMIT_CONFIGS = f"""
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY scope, COALESCE(scope_id, '{NIL_UUID}'::uuid)
               ORDER BY created_at DESC, id DESC
           ) AS rn
    FROM model.rate_limit_configs
    WHERE is_active = true
)
UPDATE model.rate_limit_configs c
   SET is_active = false
  FROM ranked r
 WHERE c.id = r.id AND r.rn > 1
"""


def upgrade() -> None:
    conn = op.get_bind()

    # Report before mutating: an operator seeing a non-zero count here knows that scope was
    # already returning 500s and that a stale budget/limit may have been in force.
    for label, sql in (
        (
            "budget.budget_configs",
            "SELECT count(*) FROM ("
            "  SELECT 1 FROM budget.budget_configs WHERE is_active = true"
            "  GROUP BY scope, scope_id, COALESCE(client, '') HAVING count(*) > 1"
            ") d",
        ),
        (
            "model.rate_limit_configs",
            "SELECT count(*) FROM ("
            "  SELECT 1 FROM model.rate_limit_configs WHERE is_active = true"
            f"  GROUP BY scope, COALESCE(scope_id, '{NIL_UUID}'::uuid) HAVING count(*) > 1"
            ") d",
        ),
    ):
        dupes = conn.exec_driver_sql(sql).scalar()
        if dupes:
            print(
                f"[0024] {label}: {dupes} key(s) had multiple active rows; "
                f"keeping the newest per key and deactivating the rest"
            )

    op.execute(DEDUPE_BUDGET_CONFIGS)
    op.execute(DEDUPE_RATE_LIMIT_CONFIGS)

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_budget_configs_active "
        "ON budget.budget_configs (scope, scope_id, COALESCE(client, '')) "
        "WHERE is_active = true"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limit_configs_active "
        f"ON model.rate_limit_configs (scope, COALESCE(scope_id, '{NIL_UUID}'::uuid)) "
        "WHERE is_active = true"
    )


def downgrade() -> None:
    # Only the indexes are dropped. The dedupe is not reverted: re-activating rows would
    # recreate the MultipleResultsFound corruption this migration exists to remove.
    op.execute("DROP INDEX IF EXISTS model.uq_rate_limit_configs_active")
    op.execute("DROP INDEX IF EXISTS budget.uq_budget_configs_active")
