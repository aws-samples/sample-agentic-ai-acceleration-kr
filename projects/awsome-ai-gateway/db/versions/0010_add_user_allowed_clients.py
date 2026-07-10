# Copyright 2026 © Amazon.com and Affiliates.
"""add auth.user_allowed_clients (per-user app allow-list)

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-20

0 rows for a user = both apps allowed (mirrors team_allowed_models allow-all).
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth.user_allowed_clients (
            user_id    UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
            client     VARCHAR(32) NOT NULL CHECK (client IN ('claude-code','cowork')),
            created_by UUID        NOT NULL REFERENCES auth.users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, client)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_allowed_clients_user "
        "ON auth.user_allowed_clients (user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth.user_allowed_clients")
