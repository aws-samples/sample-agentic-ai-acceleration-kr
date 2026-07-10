# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add nullable ttft_ms to usage.usage_logs (time to first token)

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-09

ttft_ms INTEGER (nullable): 스트리밍 요청의 첫 콘텐츠 델타 도착까지 걸린 시간(ms).
비스트리밍/미검출 요청은 latency_ms와 동일 값으로 gateway가 기록한다. 과거 데이터는
NULL(백필 없음). admin-api의 사용로그 "정상/지연" 유형 판정에 사용된다.
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE usage.usage_logs ADD COLUMN IF NOT EXISTS ttft_ms INTEGER"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE usage.usage_logs DROP COLUMN IF EXISTS ttft_ms"
    )
