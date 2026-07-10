# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add nullable availability_fallback_from to usage_logs (availability-fallback attribution)

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-22

availability_fallback_from records the original model alias when a request was
re-routed due to model availability (i.e. the model was down/throttled), as
opposed to downgraded_from which records budget-aware downgrades (FR-3.6).
Sibling column to downgraded_from: same type (VARCHAR(128) NULL), same table.
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE usage.usage_logs "
        "ADD COLUMN IF NOT EXISTS availability_fallback_from VARCHAR(128)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE usage.usage_logs "
        "DROP COLUMN IF EXISTS availability_fallback_from"
    )
