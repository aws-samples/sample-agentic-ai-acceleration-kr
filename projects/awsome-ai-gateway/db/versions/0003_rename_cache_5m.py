# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Recreate model_pricings with 5m + 1h cache columns and correct pricing data.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-29

Changes:
- DROP and recreate model.model_pricings with proper column names
- Re-seed with correct Bedrock pricing (Opus 4.7, Sonnet 4.6, Haiku 4.5)

Rationale:
The original table had `cache_creation_price_per_1k_tokens` (ambiguous) and no
1h column. Rather than patching with ADD/RENAME, recreate cleanly with both
`cache_creation_5m_price_per_1k_tokens` and `cache_creation_1h_price_per_1k_tokens`.
"""
from alembic import op


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS model.model_pricings CASCADE")
    op.execute(
        """
        CREATE TABLE model.model_pricings (
            id                                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            model_alias                         VARCHAR(128)  NOT NULL REFERENCES model.model_aliases(alias),
            input_price_per_1k_tokens           NUMERIC(10,6) NOT NULL,
            output_price_per_1k_tokens          NUMERIC(10,6) NOT NULL,
            cache_creation_5m_price_per_1k_tokens  NUMERIC(10,6) NOT NULL DEFAULT 0,
            cache_creation_1h_price_per_1k_tokens  NUMERIC(10,6) NOT NULL DEFAULT 0,
            cache_read_price_per_1k_tokens      NUMERIC(10,6) NOT NULL DEFAULT 0,
            effective_from                      TIMESTAMPTZ   NOT NULL,
            effective_until                     TIMESTAMPTZ,
            created_by                          UUID          NOT NULL REFERENCES auth.users(id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_model_pricings_alias ON model.model_pricings (model_alias, effective_from DESC)"
    )
    # Re-seed with correct Bedrock pricing
    op.execute(
        """
        INSERT INTO model.model_pricings (
            id, model_alias,
            input_price_per_1k_tokens, output_price_per_1k_tokens,
            cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
            cache_read_price_per_1k_tokens,
            effective_from, created_by
        ) VALUES
            (gen_random_uuid(), 'claude-haiku-4-5-20251001',            0.001000, 0.005000, 0.001250, 0.002000, 0.000100, '2025-10-01T00:00:00Z', '00000000-0000-4000-a000-000000000010'),
            (gen_random_uuid(), 'claude-sonnet-4-6',                  0.003000, 0.015000, 0.003750, 0.006000, 0.000300, '2025-05-14T00:00:00Z', '00000000-0000-4000-a000-000000000010'),
            (gen_random_uuid(), 'claude-sonnet-4-6[1m]',              0.003000, 0.015000, 0.003750, 0.006000, 0.000300, '2025-05-14T00:00:00Z', '00000000-0000-4000-a000-000000000010'),
            (gen_random_uuid(), 'global.anthropic.claude-opus-4-6-v1', 0.005000, 0.025000, 0.006250, 0.010000, 0.000500, '2026-05-08T00:00:00Z', '00000000-0000-4000-a000-000000000010'),
            (gen_random_uuid(), 'claude-opus-4-7',                    0.005000, 0.025000, 0.006250, 0.010000, 0.000500, '2025-05-14T00:00:00Z', '00000000-0000-4000-a000-000000000010')
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS model.model_pricings CASCADE")
    op.execute(
        """
        CREATE TABLE model.model_pricings (
            id                                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            model_alias                         VARCHAR(128)  NOT NULL REFERENCES model.model_aliases(alias),
            input_price_per_1k_tokens           NUMERIC(10,6) NOT NULL,
            output_price_per_1k_tokens          NUMERIC(10,6) NOT NULL,
            cache_creation_price_per_1k_tokens  NUMERIC(10,6) NOT NULL DEFAULT 0,
            cache_read_price_per_1k_tokens      NUMERIC(10,6) NOT NULL DEFAULT 0,
            effective_from                      TIMESTAMPTZ   NOT NULL,
            effective_until                     TIMESTAMPTZ,
            created_by                          UUID          NOT NULL REFERENCES auth.users(id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_model_pricings_alias ON model.model_pricings (model_alias, effective_from DESC)"
    )
