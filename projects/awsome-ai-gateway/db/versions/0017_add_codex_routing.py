# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add Codex routing: codex-gpt alias/pricing + routing_profiles row (in-account GPT-5.5)

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-29

Runs AFTER 0016 committed the BEDROCK_MANTLE_OPENAI / OPENAI_RESPONSES enum
values, so INSERTs using them are safe here (mirrors 0009 after 0008).

Codex client -> Bedrock Mantle GPT-5.5 via the OpenAI Responses API.
  endpoint:  https://bedrock-mantle.us-east-2.api.aws/openai  (adapter appends /v1/responses)
  model:     openai.gpt-5.5
  account:   123456789012 (SAME as gateway-proxy IRSA account) -> in-account,
             so routing_profiles.account_role_arn IS NULL (no cross-account assume,
             unlike cowork which assumes into 905). external_id NULL for the same reason.
  region:    us-east-2 (Ohio); verified live: GPT-5.5/5.4 both 200 OK in us-east-2 & us-east-1.

Pricing is a PLACEHOLDER seeded here for cost-recording wiring; refresh with the
existing AWS Price List sync feature once official GPT-5.5 Bedrock prices are mapped.
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"


def upgrade() -> None:
    # 1) codex-gpt alias (Mantle GPT-5.5, Ohio, OpenAI Responses API)
    op.execute(
        f"""
        INSERT INTO model.model_aliases
            (alias, provider, provider_model_id, endpoint_url, api_format, status,
             description, display_name, created_by)
        VALUES
            ('codex-gpt', 'BEDROCK_MANTLE_OPENAI', 'openai.gpt-5.5',
             'https://bedrock-mantle.us-east-2.api.aws/openai',
             'OPENAI_RESPONSES', 'ACTIVE',
             'Codex -> 859 Bedrock Mantle GPT-5.5 (Ohio, Responses API)',
             'Codex · GPT-5.5', '{SYSTEM_USER}')
        ON CONFLICT (alias) DO NOTHING
        """
    )

    # 2) codex-gpt pricing — PLACEHOLDER (per-1k-token USD). effective_from pinned for
    #    cross-environment price-audit parity. Refresh via AWS Price List sync.
    #    reasoning_tokens are billed inside output_tokens (OpenAI accounting) — no
    #    separate reasoning price column; reasoning is a visibility submetric only.
    op.execute(
        f"""
        INSERT INTO model.model_pricings
            (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
             cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
             cache_read_price_per_1k_tokens, effective_from, created_by)
        SELECT gen_random_uuid(), 'codex-gpt',
               0.001250, 0.010000, 0.000000, 0.000000, 0.000125,
               '2026-06-29T00:00:00Z', '{SYSTEM_USER}'
        WHERE NOT EXISTS (SELECT 1 FROM model.model_pricings WHERE model_alias = 'codex-gpt')
        """
    )

    # 3) codex routing_profiles row (in-account 859, Ohio). account_role_arn NULL =
    #    use the pod's own IRSA creds (MantleCredentialBroker in-account path), NO
    #    cross-account assume. external_id NULL. default_model forces codex-gpt.
    op.execute(
        """
        INSERT INTO model.routing_profiles
            (client, backend, account_role_arn, region, default_model, external_id, enabled)
        VALUES
            ('codex', 'mantle', NULL, 'us-east-2', 'codex-gpt', NULL, true)
        ON CONFLICT (client) DO NOTHING
        """
    )


def downgrade() -> None:
    # routing_profiles first (no FK to model_aliases), then pricing (FK -> alias), then alias.
    op.execute("DELETE FROM model.routing_profiles WHERE client = 'codex'")
    op.execute("DELETE FROM model.model_pricings WHERE model_alias = 'codex-gpt'")
    op.execute("DELETE FROM model.model_aliases WHERE alias = 'codex-gpt'")
    # enum values from 0016 are intentionally left in place (see 0016 downgrade note).
