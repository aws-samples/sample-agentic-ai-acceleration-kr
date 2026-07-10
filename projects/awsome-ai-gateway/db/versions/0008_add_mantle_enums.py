# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add Mantle enum values (provider BEDROCK_MANTLE, api_format ANTHROPIC_MESSAGES)

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-19

ENUM-ONLY by design: ALTER TYPE ADD VALUE must commit before any row uses the
new value (PostgreSQL "unsafe use of new value of enum type"). The routing
table + seed rows live in 0009 (a separate transaction).
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Irreversible ADD VALUE; IF NOT EXISTS = idempotent (PG12+).
    op.execute("ALTER TYPE model.provider ADD VALUE IF NOT EXISTS 'BEDROCK_MANTLE'")
    op.execute("ALTER TYPE model.api_format ADD VALUE IF NOT EXISTS 'ANTHROPIC_MESSAGES'")


def downgrade() -> None:
    # PostgreSQL cannot DROP an enum value without recreating the type (which
    # would break existing columns). No-op by design.
    pass
