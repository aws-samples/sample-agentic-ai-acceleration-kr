# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Add client column to usage.usage_logs for multi-app (Claude Code vs Cowork).

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-19

Nullable text column tagging which client produced each request
("claude-code" | "cowork" | "other"). Nullable with no backfill: historical
rows remain valid (NULL = pre-feature / unidentified). Indexed because the
dashboard groups/filters by client (spec §10).
"""
from alembic import op


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE usage.usage_logs ADD COLUMN IF NOT EXISTS client text")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_usage_logs_client "
        "ON usage.usage_logs (client)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS usage.ix_usage_logs_client")
    op.execute("ALTER TABLE usage.usage_logs DROP COLUMN IF EXISTS client")
