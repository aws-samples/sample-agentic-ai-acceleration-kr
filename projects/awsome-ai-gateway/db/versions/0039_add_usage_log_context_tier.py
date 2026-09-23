# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""usage_logs 에 청구 티어 감사 기록: usage.usage_logs.context_tier

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-23

## 왜 필요한가

0038 이 GPT-5.6 에 272K long-context 티어를 넣었다. 한 요청이 short 로 청구됐는지 long 으로
청구됐는지를 **요청 단위로 감사**할 수 있어야 한다 — 안 그러면 "이 요청이 왜 2배였나" 를
소명할 수 없다(입력 토큰 수만으로는 재구성이 어렵다; 캐시 포함 프롬프트 합이 임계를
넘었는지가 판정이고, usage_logs 는 그 세 버킷을 담지만 판정 결과 자체는 안 담았다).

``context_tier VARCHAR(8) NULL`` — 값: ``'short'`` / ``'long'`` / NULL(티어 없는 모델).
게이트웨이의 ``resolve_context_tier`` 가 **청구와 이 기록을 같은 판정으로** 계산해
"청구는 long, 기록은 short" 같은 소명 불가 상태를 원천 차단한다.

## 배포 순서 (hazard)

⚠️ 이 컬럼은 cost-recorder-worker 의 usage_logs INSERT 가 **명시 컬럼으로** 이름을 댄다.
   그래서 이 마이그레이션은 새 워커 이미지보다 **먼저** 적용돼야 한다 — 안 그러면 새 워커가
   없는 컬럼에 INSERT 하다 42703 으로 배치가 통째로 실패한다. (마이그레이션 Job → 이미지
   롤 순서를 지키면 됨.)

NULL 허용이라 기존 행 백필 없음(재작성 없음). 값이 없는 구버전 엔트리는 NULL 로 남고,
읽는 쪽은 NULL 을 "미상"으로 다룬다.
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

COLUMN = "context_tier"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE usage.usage_logs ADD COLUMN IF NOT EXISTS {COLUMN} VARCHAR(8)"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE usage.usage_logs DROP COLUMN IF EXISTS {COLUMN}")
