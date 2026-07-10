# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add nullable display_name to model_aliases (human-friendly 'App · Model Version' label)

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-20

display_name is a curated label shown in admin-ui (model mgmt + dashboard model-share).
NULL -> UI falls back to the raw alias. Additive + backward-compatible.
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# alias -> curated "App · Model Version" label. Cowork uses the Mantle alias; the rest are Claude Code.
_BACKFILL = {
    "cowork-opus": "Cowork · Opus 4.8",
    "claude-opus-4-8": "Claude Code · Opus 4.8",
    "global.anthropic.claude-opus-4-8": "Claude Code · Opus 4.8",
    "claude-opus-4-7": "Claude Code · Opus 4.7",
    "global.anthropic.claude-opus-4-6-v1": "Claude Code · Opus 4.6",
    "claude-sonnet-4-6": "Claude Code · Sonnet 4.6",
    "claude-sonnet-4-6[1m]": "Claude Code · Sonnet 4.6 (1M)",
    "claude-haiku-4-5-20251001": "Claude Code · Haiku 4.5",
}


def upgrade() -> None:
    op.execute("ALTER TABLE model.model_aliases ADD COLUMN IF NOT EXISTS display_name VARCHAR(128)")
    for alias, label in _BACKFILL.items():
        op.execute(
            f"UPDATE model.model_aliases SET display_name = $${label}$$ "
            f"WHERE alias = $${alias}$$ AND display_name IS NULL"
        )


def downgrade() -> None:
    op.execute("ALTER TABLE model.model_aliases DROP COLUMN IF EXISTS display_name")
