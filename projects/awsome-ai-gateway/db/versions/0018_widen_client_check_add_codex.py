# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""widen client CHECK constraints to allow 'codex' (3rd client)

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-29

The pre-existing CHECK constraints enforce client IN ('claude-code','cowork').
A new client='codex' row would be REJECTED on live/migrated DBs, so editing
init SQL alone is NOT enough — this migration drops & recreates each constraint
with 'codex' added.

Constraints widened:
  - budget.budget_configs.ck_budget_configs_client       (named)
  - budget.budget_usages.ck_budget_usages_client          (named)
  - auth.user_allowed_clients  (inline, AUTO-NAMED -> discovered via pg_constraint)
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_OLD = "('claude-code','cowork')"
_NEW = "('claude-code','cowork','codex')"


def _recreate_named(table: str, name: str, values: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(
        f"""
        DO $$ BEGIN
            ALTER TABLE {table}
                ADD CONSTRAINT {name}
                CHECK (client IS NULL OR client IN {values});
        EXCEPTION WHEN duplicate_object THEN null;
        END $$
        """
    )


def _recreate_user_allowed_clients(values: str) -> None:
    # The inline CHECK on auth.user_allowed_clients.client is auto-named by Postgres
    # (typically user_allowed_clients_client_check). Discover & drop whatever CHECK
    # constraint covers the `client` column, then add a named one we control.
    op.execute(
        """
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                WHERE ns.nspname = 'auth'
                  AND rel.relname = 'user_allowed_clients'
                  AND con.contype = 'c'
                  -- Only the CHECK that enumerates the `client` values — never touch
                  -- any unrelated CHECK that might exist on this table (Codex R2 #8).
                  AND pg_get_constraintdef(con.oid) ILIKE '%client%IN%'
            LOOP
                EXECUTE format('ALTER TABLE auth.user_allowed_clients DROP CONSTRAINT %I', c.conname);
            END LOOP;
        END $$
        """
    )
    op.execute(
        f"""
        DO $$ BEGIN
            ALTER TABLE auth.user_allowed_clients
                ADD CONSTRAINT ck_user_allowed_clients_client
                CHECK (client IN {values});
        EXCEPTION WHEN duplicate_object THEN null;
        END $$
        """
    )


def upgrade() -> None:
    _recreate_named("budget.budget_configs", "ck_budget_configs_client", _NEW)
    _recreate_named("budget.budget_usages", "ck_budget_usages_client", _NEW)
    _recreate_user_allowed_clients(_NEW)


def downgrade() -> None:
    # Revert to the 2-client form. Any existing client='codex' rows would block the
    # narrowed CHECK, so remove them first (mirrors 0011's destructive downgrade note).
    op.execute("DELETE FROM budget.budget_usages WHERE client = 'codex'")
    op.execute("DELETE FROM budget.budget_configs WHERE client = 'codex'")
    op.execute("DELETE FROM auth.user_allowed_clients WHERE client = 'codex'")
    _recreate_named("budget.budget_configs", "ck_budget_configs_client", _OLD)
    _recreate_named("budget.budget_usages", "ck_budget_usages_client", _OLD)
    _recreate_user_allowed_clients(_OLD)
