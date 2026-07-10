# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Add Claude Opus 4.8 model alias and pricing.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-30

Opus 4.8 (`global.anthropic.claude-opus-4-8` cross-region inference profile)
를 model registry 에 등록. alias 는 기존 4.7 패턴(`claude-opus-4-7`)을 따라
짧은 alias 로 전송하는 클라이언트와 full Bedrock ID 둘 다 커버.

가격: Opus 4.7 과 동일 단가로 등록(0003 의 4.7 pricing 참조). 실제 Bedrock
공시 단가가 다르면 별도 마이그레이션으로 정정할 것 — 임의 추정가는 청구
오류를 유발하므로 확정 전까지 선례(4.7) 단가를 사용.
"""
from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"


def upgrade() -> None:
    # alias: 짧은 alias (claude-opus-4-8) + full Bedrock global ID 둘 다 등록.
    op.execute(
        f"""
        INSERT INTO model.model_aliases
            (alias, provider, provider_model_id, endpoint_url, api_format, status, description, created_by)
        VALUES
            ('claude-opus-4-8', 'BEDROCK', 'global.anthropic.claude-opus-4-8', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
             'Claude Opus 4.8 1M context (Global)', '{SYSTEM_USER}'),
            ('global.anthropic.claude-opus-4-8', 'BEDROCK', 'global.anthropic.claude-opus-4-8', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
             'Claude Opus 4.8 1M context (Global, full ID)', '{SYSTEM_USER}')
        ON CONFLICT (alias) DO NOTHING
        """
    )

    # pricing — Opus 4.7 과 동일 단가 (input/output/cache).
    for alias in ("claude-opus-4-8", "global.anthropic.claude-opus-4-8"):
        op.execute(
            f"""
            INSERT INTO model.model_pricings (
                id, model_alias,
                input_price_per_1k_tokens, output_price_per_1k_tokens,
                cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
                cache_read_price_per_1k_tokens,
                effective_from, created_by
            )
            SELECT gen_random_uuid(), '{alias}',
                   0.005000, 0.025000, 0.006250, 0.010000, 0.000500,
                   '2026-05-30T00:00:00Z', '{SYSTEM_USER}'
            WHERE NOT EXISTS (
                SELECT 1 FROM model.model_pricings WHERE model_alias = '{alias}'
            )
            """
        )


def downgrade() -> None:
    for alias in ("claude-opus-4-8", "global.anthropic.claude-opus-4-8"):
        op.execute(f"DELETE FROM model.model_pricings WHERE model_alias = '{alias}'")
        op.execute(f"DELETE FROM model.model_aliases WHERE alias = '{alias}'")
