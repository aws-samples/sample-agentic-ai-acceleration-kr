# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Add Claude Opus 4.6 model alias and pricing.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-08

Claude Code v2.1.131+ sends model body as 'global.anthropic.claude-opus-4-6-v1'
(full Bedrock model ID, not a short alias). Register it as-is so gateway-proxy
can resolve it.
"""
from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO model.model_aliases (alias, provider, provider_model_id, endpoint_url, api_format, status, description, created_by)
        VALUES ('global.anthropic.claude-opus-4-6-v1', 'BEDROCK', 'global.anthropic.claude-opus-4-6-v1', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
                'Claude Opus 4.6 1M context (Global)',
                '00000000-0000-4000-a000-000000000010')
        ON CONFLICT (alias) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO model.model_pricings (
            id, model_alias,
            input_price_per_1k_tokens, output_price_per_1k_tokens,
            cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
            cache_read_price_per_1k_tokens,
            effective_from, created_by
        )
        SELECT gen_random_uuid(), 'global.anthropic.claude-opus-4-6-v1',
               0.005000, 0.025000, 0.006250, 0.010000, 0.000500,
               '2026-05-08T00:00:00Z', '00000000-0000-4000-a000-000000000010'
        WHERE NOT EXISTS (
            SELECT 1 FROM model.model_pricings WHERE model_alias = 'global.anthropic.claude-opus-4-6-v1'
        )
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM model.model_pricings WHERE model_alias = 'global.anthropic.claude-opus-4-6-v1'")
    op.execute("DELETE FROM model.model_aliases WHERE alias = 'global.anthropic.claude-opus-4-6-v1'")
