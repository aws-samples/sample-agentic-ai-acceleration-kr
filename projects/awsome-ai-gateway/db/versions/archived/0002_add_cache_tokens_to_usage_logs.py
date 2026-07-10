# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add cache_creation_tokens and cache_read_tokens to usage.usage_logs

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-17

`gateway-proxy/src/app/models/usage.py:UsageRecord` ORM이
`cache_creation_tokens`, `cache_read_tokens` 두 컬럼을 INSERT 시도하나
실제 테이블에 없어서 매 성공 요청마다 `usage_record_save_failed` 에러
발생 + 비용 기록이 DB에 쌓이지 않음. Anthropic Prompt Caching 비용
(cache_creation 1.25x, cache_read 0.1x) 정확 추적의 전제.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage_logs",
        sa.Column(
            "cache_creation_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="usage",
    )
    op.add_column(
        "usage_logs",
        sa.Column(
            "cache_read_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="usage",
    )


def downgrade() -> None:
    op.drop_column("usage_logs", "cache_read_tokens", schema="usage")
    op.drop_column("usage_logs", "cache_creation_tokens", schema="usage")
