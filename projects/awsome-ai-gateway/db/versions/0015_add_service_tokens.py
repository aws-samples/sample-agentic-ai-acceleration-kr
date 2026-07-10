# Copyright 2026 © Amazon.com and Affiliates.
"""add auth.service_tokens (external-system bearer tokens for admin-api)

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-26

External systems call admin-api with `Authorization: Bearer svc-...`. We store
only the sha256 hash of the token; the plaintext is returned once at issue/rotate.
admin-full权限. expiry required (default 90d); rotate gives a 24h grace on the old
token. (260626_comm_customer item 1 — external admin-api integration.)
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth.service_tokens (
            id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            name         VARCHAR(128) NOT NULL,
            token_hash   VARCHAR(64)  NOT NULL UNIQUE,
            token_prefix VARCHAR(16)  NOT NULL,
            created_by   UUID         NOT NULL REFERENCES auth.users(id),
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ  NOT NULL,
            revoked_at   TIMESTAMPTZ,
            rotated_from UUID         REFERENCES auth.service_tokens(id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_service_tokens_hash "
        "ON auth.service_tokens (token_hash)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth.service_tokens")
