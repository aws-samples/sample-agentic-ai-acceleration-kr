# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add budget.downgrade_policies for FR-3.6 Budget-aware 모델 다운그레이드

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-17

사용자/팀의 월 예산 소진율이 Admin 설정 임계치(예: 80%)를
초과하면, 상위 모델(from_model_alias) 요청을 하위 모델(to_model_alias)로
자동 전환하여 비용 초과 방지.

설계:
- `scope` + `scope_id` 패턴으로 기존 budget_configs/budget_usages와 일관.
  USER 또는 TEAM 단위 정책.
- `threshold_pct` (1~100): 예산 소진율이 이 값 이상일 때 다운그레이드 발동.
- `from_model_alias` / `to_model_alias`: 변환 매핑. FK로 실제 존재하는
  alias만 허용. self-loop 방지 CHECK.
- `is_active` partial index로 효율적 조회.
- **one-hop only**: A→B, B→C 체인은 MVP 범위 밖. 조회 시 to_model_alias가
  또 downgrade 대상이어도 follow 하지 않음.
- **다수 정책 중복 가능**: 한 사용자에게 여러 from_model_alias 매핑 가능
  (예: opus→sonnet + sonnet→haiku). 같은 (scope,scope_id,from_model_alias)
  중복은 의미 없으므로 partial unique index 추가.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "downgrade_policies",
        sa.Column(
            "id",
            UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "scope",
            sa.Enum(
                "USER",
                "TEAM",
                name="budget_scope",
                schema="budget",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("scope_id", UUID(as_uuid=False), nullable=False),
        sa.Column("threshold_pct", sa.Integer(), nullable=False),
        sa.Column("from_model_alias", sa.String(length=128), nullable=False),
        sa.Column("to_model_alias", sa.String(length=128), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_by", UUID(as_uuid=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "threshold_pct BETWEEN 1 AND 100",
            name="ck_downgrade_threshold_range",
        ),
        sa.CheckConstraint(
            "from_model_alias <> to_model_alias",
            name="ck_downgrade_no_self_loop",
        ),
        sa.ForeignKeyConstraint(
            ["from_model_alias"], ["model.model_aliases.alias"]
        ),
        sa.ForeignKeyConstraint(
            ["to_model_alias"], ["model.model_aliases.alias"]
        ),
        sa.ForeignKeyConstraint(["created_by"], ["auth.users.id"]),
        schema="budget",
    )
    op.create_index(
        "idx_downgrade_policies_scope",
        "downgrade_policies",
        ["scope", "scope_id"],
        schema="budget",
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "idx_downgrade_policies_unique_active",
        "downgrade_policies",
        ["scope", "scope_id", "from_model_alias"],
        schema="budget",
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_downgrade_policies_unique_active",
        table_name="downgrade_policies",
        schema="budget",
    )
    op.drop_index(
        "idx_downgrade_policies_scope",
        table_name="downgrade_policies",
        schema="budget",
    )
    op.drop_table("downgrade_policies", schema="budget")
