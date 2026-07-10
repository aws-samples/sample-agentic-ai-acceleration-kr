# Copyright 2026 © Amazon.com and Affiliates.
"""add model.user_allowed_models (per-user model allow-list, overrides team)

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-26

Per-user model access control. Precedence is resolved at AuthContext-snapshot
time (admin key_service + gateway auth_service): user > team > none.
  - user has rows  -> use this whitelist (team ignored)
  - user has 0 rows -> fall back to team_allowed_models (override cleared)
Unlike team_allowed_models, 0 rows here means "fall back to team", NOT "allow all".
(260626_comm_customer item 2 — national-core-tech per-individual restriction.)
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model.user_allowed_models (
            user_id      UUID         NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
            model_alias  VARCHAR(128) NOT NULL REFERENCES model.model_aliases(alias),
            created_by   UUID         NOT NULL REFERENCES auth.users(id),
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, model_alias)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_allowed_models_user "
        "ON model.user_allowed_models (user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS model.user_allowed_models")
