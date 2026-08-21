# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""index usage_logs.requested_at for the KST period range filter

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-14

대시보드/analytics/budget/my 의 기간 필터는 usage_logs 를 **requested_at 만으로** 좁힌다
(app/core/usage_filters.py: cost_period_filter → period_to_utc_range). 그런데 기존 인덱스는
전부 다른 컬럼이 선행하는 복합 인덱스라 이 필터에 쓸 수 없었다:

    idx_usage_logs_user_time   (user_id,     requested_at DESC)
    idx_usage_logs_team_time   (team_id,     requested_at DESC)
    idx_usage_logs_model_time  (model_alias, requested_at DESC)

B-tree 는 선행 컬럼에 조건이 없으면 탐색을 시작할 지점을 못 찾는다. 그래서 user/team 없이
"이번 달 전체"를 묻는 쿼리(대시보드 요약, /periods, ROI 팀 탐색)는 매번 전체 스캔이었다.
이 인덱스가 그 지점을 메운다.

같은 릴리스에서 필터를 sargable 형태로 바꿨다(과거:
`to_char(timezone('Asia/Seoul', requested_at),'YYYY-MM') = :period` — 컬럼이 함수 안에
갇혀 인덱스 사용 불가). **두 변경은 짝**이다: 필터만 고치면 쓸 인덱스가 없고, 인덱스만
만들면 필터가 여전히 못 쓴다.

실측(598,808행, PostgreSQL 16, 워밍업 2회 후 6회 중위값): Parallel Seq Scan 86.7ms →
Index Scan 7.9ms (약 11배). 캐시와 무관한 지표로는 touched buffers 15,373 → 1,811.
인덱스 생성 자체는 139ms.

⚠️ 배수는 page cache 상태에 민감하다. cold 첫 실행은 1,086ms / 159ms 로 둘 다 크게
튀므로, cold 와 warm 을 섞어 비교하면 5배~15배로 요동친다. 재측정 시 워밍업 후
동일 조건으로 비교하고, 가능하면 buffer 수를 함께 볼 것.

⚠️ CREATE INDEX **CONCURRENTLY 를 쓰지 않는다.** db/env.py 가
`transaction_per_migration=True` 로 각 마이그레이션을 트랜잭션에 감싸는데, CONCURRENTLY 는
트랜잭션 블록 안에서 실행할 수 없다(PostgreSQL 제약). 일반 CREATE INDEX 는 그 시간 동안
usage_logs 에 대한 쓰기를 막지만(ACCESS EXCLUSIVE 가 아니라 쓰기만 차단하는 SHARE 락),
실측 139ms 이고 usage 쓰기는 게이트웨이의 버퍼된 백그라운드 경로라 잠깐의 지연을 흡수한다.
행 수가 수천만 규모로 커져 이 시간이 문제가 되면, 그때는 이 마이그레이션 밖에서
CONCURRENTLY 로 수동 생성한 뒤 IF NOT EXISTS 로 no-op 되게 하는 편이 안전하다.

DESC 로 만드는 이유: 범위 스캔에는 방향이 무관하지만, 이 컬럼의 다른 쓰임새(최근 N건
조회)가 전부 최신순이고 기존 복합 인덱스들도 DESC 라 관례를 맞춘다.
"""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_logs_requested_at "
        "ON usage.usage_logs (requested_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS usage.idx_usage_logs_requested_at")
