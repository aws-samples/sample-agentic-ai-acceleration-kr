# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add Codex enum values (provider BEDROCK_MANTLE_OPENAI, api_format OPENAI_RESPONSES)

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-29

ENUM-ONLY by design (mirrors 0008): ALTER TYPE ADD VALUE must commit before any
row uses the new value (PostgreSQL "unsafe use of new value of enum type"). The
Codex routing table rows (alias/pricing/routing_profile) live in 0017.

Codex = OpenAI Codex CLI -> Bedrock Mantle GPT-5.5 via the OpenAI Responses API
(POST /openai/v1/responses), in-account in 123456789012 / us-east-2.
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Irreversible ADD VALUE; IF NOT EXISTS = idempotent (PG12+).
    op.execute("ALTER TYPE model.provider ADD VALUE IF NOT EXISTS 'BEDROCK_MANTLE_OPENAI'")
    op.execute("ALTER TYPE model.api_format ADD VALUE IF NOT EXISTS 'OPENAI_RESPONSES'")


def downgrade() -> None:
    # PostgreSQL cannot DROP an enum value without recreating the type (which
    # would break existing columns). No-op by design (same as 0008).
    pass
