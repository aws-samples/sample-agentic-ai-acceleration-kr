# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""baseline — 통합 init SQL 스냅샷 + default team budget backfill

Revision ID: 0001
Revises:
Create Date: 2026-04-18 (consolidated 2026-04-26 — OIDC provider 컬럼까지 통합)

배경
----
개발 단계에서는 `db/init/*.sql`이 최종 스키마의 source of truth.
auth.users.provider, auth.users.user_arn 컬럼도 init SQL 에 포함됨.
Alembic은 revision 추적 + 시드/backfill 만 담당하며 실제 DDL은 수행하지 않는다.

향후 pre-prod 진입 시점부터:
- init SQL은 baseline v1으로 freeze
- 이후 변경은 0002_*, 0003_* 증분 migration으로 작성
- 고객 on-prem upgrade 시 alembic upgrade로 데이터 보존하며 진행

이 baseline 의 upgrade 작업
-----------------------
팀이 생성되었으나 TEAM-scope 활성 budget_configs 행이 없는 경우
BUDGET_TEAM_DEFAULT_USD (기본값 1000 USD) 로 HARD_BLOCK/MONTHLY 예산을 자동 생성한다.
C-1 정책 (TEAM 미설정 = deny enforcement) 의 prerequisite.

- allocated_by: 시드 데이터(03_seed_data.sql)의 시스템 관리자 UUID
  (00000000-0000-4000-a000-000000000010) 를 sentinel 로 사용. FK 위반 없음.
- updated_at 컬럼은 budget.budget_configs 에 존재하지 않음 (created_at 만 있음).
- WHERE NOT EXISTS 로 멱등성 보장 (재실행 시 중복 삽입 없음).
"""

from __future__ import annotations

import os

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# 시드 데이터(03_seed_data.sql)에서 항상 존재가 보장되는 시스템 관리자 UUID.
# allocated_by NOT NULL FK 제약을 충족하기 위한 sentinel 값.
_SYSTEM_ADMIN_UUID = "00000000-0000-4000-a000-000000000010"


def upgrade() -> None:
    """Init SQL 이후 default TEAM budget backfill (멱등)."""
    raw = os.environ.get("BUDGET_TEAM_DEFAULT_USD", "1000")
    try:
        default_usd = float(raw)
        if default_usd <= 0:
            raise ValueError("must be positive")
    except ValueError as exc:
        raise SystemExit(
            f"[migration] BUDGET_TEAM_DEFAULT_USD={raw!r} is invalid: {exc}"
        ) from exc

    op.execute(
        f"""
        INSERT INTO budget.budget_configs
            (id, scope, scope_id, max_budget_usd, policy, period_type,
             is_active, allocated_by, effective_from, created_at)
        SELECT gen_random_uuid(),
               'TEAM'::budget.budget_scope,
               t.id,
               CAST({default_usd} AS NUMERIC),
               'HARD_BLOCK'::budget.budget_policy,
               'MONTHLY'::budget.period_type,
               true,
               '{_SYSTEM_ADMIN_UUID}'::uuid,
               CURRENT_DATE,
               NOW()
        FROM auth.teams t
        WHERE NOT EXISTS (
            SELECT 1 FROM budget.budget_configs bc
            WHERE bc.scope = 'TEAM'::budget.budget_scope
              AND bc.scope_id = t.id
              AND bc.is_active = true
        );
        """
    )


def downgrade() -> None:
    """No-op: baseline 롤백은 volume drop 으로 처리."""
    pass
