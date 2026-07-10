# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add nullable client column to budget_configs + budget_usages (per-app budgets)

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-20

client IS NULL = existing USER/TEAM total/team row (unchanged, no backfill).
client IN ('claude-code','cowork') = per-app sub-limit (scope=USER only).
Unique index uses COALESCE(client,'') so NULL totals don't duplicate.
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # budget_configs: add nullable client column + check constraint
    op.execute("ALTER TABLE budget.budget_configs ADD COLUMN IF NOT EXISTS client VARCHAR(32)")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE budget.budget_configs
                ADD CONSTRAINT ck_budget_configs_client
                CHECK (client IS NULL OR client IN ('claude-code','cowork'));
        EXCEPTION WHEN duplicate_object THEN null;
        END $$
        """
    )

    # budget_usages: add nullable client column + check constraint
    op.execute("ALTER TABLE budget.budget_usages ADD COLUMN IF NOT EXISTS client VARCHAR(32)")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE budget.budget_usages
                ADD CONSTRAINT ck_budget_usages_client
                CHECK (client IS NULL OR client IN ('claude-code','cowork'));
        EXCEPTION WHEN duplicate_object THEN null;
        END $$
        """
    )

    # Swap the unique index to include COALESCE(client,'') (NULL-stable sentinel).
    # NULL is treated as '' so existing total rows (client IS NULL) are not duplicated.
    op.execute("DROP INDEX IF EXISTS budget.idx_budget_usages_unique")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_usages_unique "
        "ON budget.budget_usages (scope, scope_id, period, COALESCE(client,''))"
    )


def downgrade() -> None:
    # ORDER MATTERS: per-app rows (client IS NOT NULL) share (scope, scope_id, period)
    # with the client=NULL total row, so they are distinct ONLY under the 4-col
    # COALESCE(client,'') index. We must remove per-app rows and the client column
    # BEFORE recreating the coarse 3-col unique index, or the CREATE UNIQUE INDEX
    # aborts on duplicate (scope, scope_id, period) tuples (the worker writes total +
    # per-app rows for the same key — see cost-recorder-worker batch_flusher).
    op.execute("DROP INDEX IF EXISTS budget.idx_budget_usages_unique")
    # Drop per-app usage rows first (configs keep history; usages are the colliding set).
    op.execute("DELETE FROM budget.budget_usages WHERE client IS NOT NULL")
    op.execute(
        "ALTER TABLE budget.budget_usages DROP CONSTRAINT IF EXISTS ck_budget_usages_client"
    )
    op.execute("ALTER TABLE budget.budget_usages DROP COLUMN IF EXISTS client")
    op.execute(
        "ALTER TABLE budget.budget_configs DROP CONSTRAINT IF EXISTS ck_budget_configs_client"
    )
    op.execute("ALTER TABLE budget.budget_configs DROP COLUMN IF EXISTS client")
    # Now that client is gone from budget_usages, the remaining rows are unique on
    # (scope, scope_id, period) → safe to recreate the pre-0011 coarse index.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_usages_unique "
        "ON budget.budget_usages (scope, scope_id, period)"
    )
