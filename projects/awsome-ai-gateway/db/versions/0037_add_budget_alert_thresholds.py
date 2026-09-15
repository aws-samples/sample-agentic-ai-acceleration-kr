# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""운영자가 설정한 예산 알림 임계값을 실제로 저장한다: budget_configs.alert_thresholds

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-15

## 왜 필요한가 — 기능이 DB 앞에서 끊겨 있었다

임계값 알림 설정은 **UI 부터 Redis 까지 이미 다 있었다.** 없는 것은 저장소뿐이다:

  * ``admin-ui`` 의 SetBudgetDialog 는 임계값을 추가/삭제하는 편집기를 제공하고,
    ``api.ts`` 는 ``alert_thresholds`` 를 1~100 정수 배열로 검증한다.
  * ``admin-api`` 의 ``BudgetCreate.alert_thresholds`` 가 그것을 받고, 예산을 저장할 때
    Redis 설정 JSON 에 ``"thresholds": sorted(alert_thresholds)`` 로 써 넣는다.
  * ``budget_deduct.lua`` 는 그 ``config.thresholds`` 를 읽어 교차를 판정한다.

그런데 ``budget.budget_configs`` 에는 그 값을 담을 컬럼이 없었다. admin-api 의 코드가
그 사실을 직접 적어 두고 있었다 — ``alert_thresholds=[80, 90, 100],  # DB에 컬럼 없음``.

결과는 **조용히 되돌아가는 설정**이다. 운영자가 50% 알림을 추가하면 admin-api 가 Redis
설정 키에 그것을 쓰고 알림이 실제로 동작한다. 그 키의 TTL 은 300초다. 만료되면
gateway-proxy 의 재수화 경로(``budget_service._hydrate_*_config_cache``)가 DB 를 읽어
설정을 다시 만드는데, 저장된 임계값이 없으므로 ``DEFAULT_THRESHOLDS`` = [80, 90, 100] 을
써 넣는다.

즉 운영자의 설정은 **최대 5분 동안만** 살아 있고, 그 뒤로는 오류도 경고도 없이 기본값으로
돌아간다. 화면에는 여전히 50% 가 저장된 것으로 보인다. 알림이 오지 않는다는 사실을
알아챌 방법이 없다 — 알림의 부재는 "예산을 안 썼다" 와 구별되지 않는다.

## 기본값을 '{80,90,100}' 으로 두는 이유

``NOT NULL DEFAULT '{80,90,100}'`` 은 기존 행에 지금 코드가 쓰는 값과 **같은** 값을 채운다.
그래서 이 마이그레이션 자체로 동작이 바뀌는 행은 없다(적용 시점의 알림 거동은 동일).
바뀌는 것은 이제부터 운영자가 넣는 값이 살아남는다는 것이다.

NULL 을 허용하지 않는 것도 의도다. NULL 이면 읽는 쪽마다 "NULL = 기본값" 규칙을 각자
구현해야 하고, 한 곳이 빠지면 임계값이 빈 목록으로 읽혀 **알림이 통째로 사라진다** —
그 방향의 실패는 조용하다.

## 빈 배열은 "알림 없음" 이다

``'{}'`` 는 유효한 값이고 "이 예산에는 임계값 알림을 보내지 않는다" 를 뜻한다. 운영자가
명시적으로 비운 것과, 저장에 실패해 비어 있는 것을 구별할 수 있어야 하므로 그 구별을
DEFAULT 가 아니라 **NOT NULL** 로 만든다(쓰지 않으면 기본값이 들어가고, 비우려면 빈
배열을 명시해야 한다).

## 값 검증 — CHECK 이 아니라 도메인으로

1..100 범위를 강제하는데, ``CHECK`` 절에는 **서브쿼리를 쓸 수 없다**. 처음 쓴
``CHECK (alert_thresholds <@ ARRAY(SELECT generate_series(1,100))::INTEGER[])`` 는 실 PG16
에서 거부되고, ``ON_ERROR_STOP=1`` 아래의 init SQL 에서는 그 뒤 문장이 전부 버려진다.

PostgreSQL 은 **도메인 제약을 배열 원소마다** 적용하므로 ``budget.alert_pct`` 도메인이
정확히 맞는 도구다(실측: ``'{150}'`` 과 ``'{0,80}'`` 거부, ``'{}'`` 와 기본값 허용).

UI 와 API 스키마가 이미 같은 범위를 검증한다. 그럼에도 DB 에서 막는 이유는 그 둘을 거치지
않는 경로(직접 SQL, 시드 스크립트, 데이터 보정)가 있고, 잘못된 값의 증상이 조용하기
때문이다 — 0 이나 150 은 Lua 의 교차 판정에서 각각 "항상 발동" 과 "절대 발동 안 함" 이 된다.

⚠️ ``0`` 을 막는 것이 특히 중요하다. ``old_pct < 0`` 은 성립할 수 없으므로 0 은 발동하지
   않는 것처럼 보이지만, 사용량이 0 인 상태에서 첫 요청이 오면 ``old_pct = 0`` 이고
   ``new_pct >= 0`` 이라 **모든 첫 요청마다** 알림이 나간다.
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ⚠️ CHECK 절에는 서브쿼리를 쓸 수 없다 — ``<@ ARRAY(SELECT generate_series(1,100))``
    #    는 "cannot use subquery in check constraint" 로 거부된다(실 PG16 에서 실측했고,
    #    ON_ERROR_STOP 아래에서는 그 뒤 문장이 전부 버려진다). PostgreSQL 은 도메인 제약을
    #    **배열 원소마다** 적용하므로 도메인이 정확히 필요한 도구다.
    op.execute(
        """
        DO $$
        BEGIN
            CREATE DOMAIN budget.alert_pct AS INTEGER CHECK (VALUE BETWEEN 1 AND 100);
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE budget.budget_configs
            ADD COLUMN IF NOT EXISTS alert_thresholds budget.alert_pct[]
            NOT NULL DEFAULT '{80,90,100}'
        """
    )
    # 이 컬럼을 INTEGER[] 로 먼저 만든 DB(초기 시도)가 있을 수 있다 — 타입을 맞춰 준다.
    # 값이 범위를 벗어나 있으면 여기서 실패하는 것이 맞다(조용히 남겨 두면 안 된다).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'budget'
                  AND table_name = 'budget_configs'
                  AND column_name = 'alert_thresholds'
                  AND udt_name = '_int4'
            ) THEN
                ALTER TABLE budget.budget_configs
                    ALTER COLUMN alert_thresholds TYPE budget.alert_pct[];
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    # ⚠️ 컬럼을 떨어뜨리면 운영자가 설정한 임계값이 **사라진다**. 다시 upgrade 하면 모든
    #    예산이 기본값 [80,90,100] 으로 돌아간다 — 즉 이 downgrade 는 데이터 손실이고,
    #    되돌린 뒤 재적용으로 복구되지 않는다. 손실 범위가 "알림 임계값" 으로 한정되고
    #    예산 금액/정책은 무영향이므로 허용 가능한 교환이다.
    op.execute(
        "ALTER TABLE budget.budget_configs DROP COLUMN IF EXISTS alert_thresholds"
    )
    # 도메인은 컬럼이 사라진 뒤에만 떨어진다. 다른 컬럼이 쓰고 있으면 RESTRICT 로 남는다.
    op.execute("DROP DOMAIN IF EXISTS budget.alert_pct")
