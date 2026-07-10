# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add model.team_allowed_models for FR-2.6 team-scoped model access

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-17

Admin이 팀별로 호출 가능한 모델 목록을 설정.
- 빈 엔트리 (해당 team_id 기준 행 없음) = 전체 허용 (하위 호환)
- 엔트리 존재 = 해당 엔트리의 model_alias만 허용 (화이트리스트)

VK 발급 시 admin-api가 이 테이블을 조회하여 `AuthContext.allowed_models`를
채워 Redis에 캐시 → gateway-proxy의 `RouterService._check_key_scope`가 이미
검증 로직 보유 (`allowed_models`가 None/empty이면 통과, 값이 있으면 화이트리스트).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_allowed_models",
        sa.Column("team_id", UUID(as_uuid=False), nullable=False),
        sa.Column("model_alias", sa.String(length=128), nullable=False),
        sa.Column("created_by", UUID(as_uuid=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("team_id", "model_alias"),
        sa.ForeignKeyConstraint(
            ["team_id"], ["auth.teams.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["model_alias"], ["model.model_aliases.alias"]
        ),
        sa.ForeignKeyConstraint(["created_by"], ["auth.users.id"]),
        schema="model",
    )
    op.create_index(
        "idx_team_allowed_models_team",
        "team_allowed_models",
        ["team_id"],
        schema="model",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_team_allowed_models_team",
        table_name="team_allowed_models",
        schema="model",
    )
    op.drop_table("team_allowed_models", schema="model")
