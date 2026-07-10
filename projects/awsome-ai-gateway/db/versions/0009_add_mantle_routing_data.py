# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add Mantle routing: routing_profiles table + cowork-opus alias/pricing/profile

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-19

Runs AFTER 0008 committed the BEDROCK_MANTLE / ANTHROPIC_MESSAGES enum values,
so INSERTs using them are safe here.
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"


def upgrade() -> None:
    # 1) routing_profiles table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model.routing_profiles (
            client            text PRIMARY KEY,
            backend           text NOT NULL,
            account_role_arn  text,
            region            text NOT NULL,
            default_model     text,
            external_id       text,
            enabled           boolean NOT NULL DEFAULT true,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT routing_profiles_backend_check CHECK (backend IN ('invoke','mantle'))
        )
        """
    )

    # 2) cowork-opus alias (Mantle Opus 4.8, Tokyo)
    op.execute(
        f"""
        INSERT INTO model.model_aliases
            (alias, provider, provider_model_id, endpoint_url, api_format, status, description, created_by)
        VALUES
            ('cowork-opus', 'BEDROCK_MANTLE', 'anthropic.claude-opus-4-8',
             'https://bedrock-mantle.ap-northeast-1.api.aws/anthropic',
             'ANTHROPIC_MESSAGES', 'ACTIVE',
             'Cowork -> 905 Bedrock Mantle Opus 4.8 (Tokyo)', '{SYSTEM_USER}')
        ON CONFLICT (alias) DO NOTHING
        """
    )

    # 3) cowork-opus pricing — EXACT Opus 4.8 values from 0006 (per-1k-token USD).
    #    effective_from pinned (not now()) for cross-environment parity in price audits.
    op.execute(
        f"""
        INSERT INTO model.model_pricings
            (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
             cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
             cache_read_price_per_1k_tokens, effective_from, created_by)
        SELECT gen_random_uuid(), 'cowork-opus',
               0.005000, 0.025000, 0.006250, 0.010000, 0.000500,
               '2026-06-19T00:00:00Z', '{SYSTEM_USER}'
        WHERE NOT EXISTS (SELECT 1 FROM model.model_pricings WHERE model_alias = 'cowork-opus')
        """
    )

    # 4) cowork routing_profiles row (905 Mantle, Tokyo)
    op.execute(
        """
        INSERT INTO model.routing_profiles
            (client, backend, account_role_arn, region, default_model, external_id, enabled)
        VALUES
            ('cowork', 'mantle',
             'arn:aws:iam::234567890123:role/llm-gateway-cowork-bedrock',
             'ap-northeast-1', 'cowork-opus', 'cowork-bedrock', true)
        ON CONFLICT (client) DO NOTHING
        """
    )


def downgrade() -> None:
    # Drop routing_profiles before touching the alias/pricing rows. routing_profiles
    # has no FK to model_aliases (default_model is plain text), so dropping it first is
    # safe and avoids any future FK-ordering hazard. Pricing (FK -> model_aliases.alias)
    # MUST be deleted before the alias row.
    op.execute("DELETE FROM model.routing_profiles WHERE client = 'cowork'")
    op.execute("DROP TABLE IF EXISTS model.routing_profiles")
    op.execute("DELETE FROM model.model_pricings WHERE model_alias = 'cowork-opus'")
    op.execute("DELETE FROM model.model_aliases WHERE alias = 'cowork-opus'")
    # enum values from 0008 are intentionally left in place (see 0008 downgrade note).
