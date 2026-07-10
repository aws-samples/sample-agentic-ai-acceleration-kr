# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add reasoning_tokens to usage_logs (3-client reasoning visibility submetric)

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-29

reasoning_tokens is a VISIBILITY SUBMETRIC, NOT a billing input. OpenAI/GPT-5.5
already counts reasoning tokens INSIDE output_tokens, so reasoning_tokens must
NOT be added to total/cost/TPM again (double-billing). It is recorded for
analytics + UI display across all clients (claude-code / cowork / codex);
Anthropic extended-thinking tokens land here too when present, else 0.

Additive + backward-compatible: DEFAULT 0, existing rows unaffected.
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE usage.usage_logs "
        "ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE usage.usage_logs DROP COLUMN IF EXISTS reasoning_tokens")
