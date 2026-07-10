# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add web_search_count to usage_logs (server-side web-search attribution metric)

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-01

web_search_count is a VISIBILITY/ATTRIBUTION metric, NOT a billing input or token
count. The server-side web-search loop (services/web_search_loop.py) increments it
once per successful AgentCore Gateway WebSearch call while serving one client
request. Like reasoning_tokens (0019), it must NOT be added to total_tokens/cost/
TPM. It lets the dashboard attribute AgentCore search usage ($7/1k) per client.

Additive + backward-compatible: DEFAULT 0, existing rows unaffected.
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE usage.usage_logs "
        "ADD COLUMN IF NOT EXISTS web_search_count INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE usage.usage_logs DROP COLUMN IF EXISTS web_search_count")
