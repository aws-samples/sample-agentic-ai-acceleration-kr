# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""컨텍스트 밴드 요금제: 프롬프트가 임계를 넘으면 요청 전체를 long 요율로 청구 (명시 요율)

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-23

## 무엇을 넣나

GPT-5.6 (terra/sol/luna) 모델카드는 요금표를 **크기축으로** 게시한다. OpenAI 문구를
AWS 카드가 그대로 인용한다:

    "Prompts with >272K input tokens are priced at 2x input and 1.5x output
     for the full request."

한 요청 프롬프트가 272,000 토큰을 넘으면 그 요청 **전체**가 long 요율로 재청구된다
(계단 하나, 초과분 누진 아님; 273K 와 999K 는 같은 요율). 카드 배수:
input x2, cache(read/write) x2, output x1.5.

``model_pricings`` 에는 크기축이 없어서 272K 초과 요청이 절반 요율로 과소청구됐다
(0025 주석이 지적, 미착수였다). 이 마이그레이션이 크기축을 넣는다.

## 왜 배수가 아니라 **명시 요율 컬럼**인가

short 요율과 같은 단위(per-1k Decimal)로 5개 long 컬럼을 둔다:

    long_context_threshold_tokens                            INTEGER  NULL
    long_context_input_price_per_1k_tokens                   NUMERIC(10,6) NULL
    long_context_output_price_per_1k_tokens                  NUMERIC(10,6) NULL
    long_context_cache_creation_5m_price_per_1k_tokens       NUMERIC(10,6) NULL
    long_context_cache_creation_1h_price_per_1k_tokens       NUMERIC(10,6) NULL
    long_context_cache_read_price_per_1k_tokens              NUMERIC(10,6) NULL

배수(long = short × 2) 대신 명시 요율을 쓰는 이유는 **AWS Price List 자동연동** 때문이다
(후속 PR): Price List 는 배수가 아니라 달러 요율을 준다. 명시 컬럼이라야 sync 가 fetch 한
값을 그대로 넣을 수 있다. 컬럼명은 pub 의 기존 short 컬럼과 동일 패턴이라 깔끔하게 붙는다.

## ⚠️ Claude 는 티어가 없다

Anthropic 4.6+ 는 1M 컨텍스트를 표준 단가로 청구한다(Bedrock 에 Anthropic long-ctx SKU
0건). ``threshold`` 가 NULL 이면 티어 없음 = 오늘 동작. 티어를 켜는 대상은
``provider_model_id LIKE '%openai.gpt-5.6-%'`` 인 행뿐이다.

## 시딩 — 배수 산술 × 패턴 매칭 (phase2 하드코딩 절대값을 쓰지 않는 이유)

phase2 원본은 alias 별 **절대 long 요율**을 하드코딩하고 runtime 트랙을 Global CRIS 로
가정했다. 그러나 pub 의 runtime alias(gpt-5.6-*)는 0032 에서 **In-Region** short 로
시드됐다(mantle 과 동일) — phase2 의 Global CRIS 절대값과 어긋난다. 그래서 절대값을
베끼는 대신, 열린 각 gpt-5.6 행의 **현재 short 요율에 카드 배수를 곱해** long 을 채운다:

    long_input  = input  × 2
    long_output = output × 1.5
    long_cache_* = cache_* × 2

이렇게 하면 pub 이 실제로 쓰는 short 요율(0033 의 sol 프로모 포함)을 정확히 추적하고,
alias 이름/plane 에 무관하다. (long 이 short 변경을 자동 추적하지 않는 명시-요율의 한계는
자동연동 PR 이 재fetch 로 해소한다.)

## 활성화 가드 — threshold IS NULL

열린 행(effective_until IS NULL) 중 아직 티어가 없는(threshold IS NULL) 것만 채운다.
  * 재실행 안전(멱등): 이미 채운 행은 제외된다.
  * 운영자 안전: UI/sync 로 티어를 이미 설정한 환경은 threshold 가 NULL 이 아니라 무시된다.

⚠️ 요율 행을 UPDATE 하는 것이 0030 의 "요율 UPDATE 금지" 규약과 충돌하지 않는 이유:
   long 컬럼은 이 마이그레이션 이전에 존재하지 않았으므로(NULL) 다른 값을 가졌던 과거
   기간이 없다 — NULL→값 은 청구 이력을 파괴하지 않는다. 그리고 model_pricings 의 부분
   유니크 (model_alias) WHERE effective_until IS NULL(0034) 때문에 새 열린 행을 넣을 수
   없다. 티어는 미래 요청부터만 영향(usage_logs 불변, 소급 재청구 없음).
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

THRESHOLD_COL = "long_context_threshold_tokens"
PRICE_COLS = (
    "long_context_input_price_per_1k_tokens",
    "long_context_output_price_per_1k_tokens",
    "long_context_cache_creation_5m_price_per_1k_tokens",
    "long_context_cache_creation_1h_price_per_1k_tokens",
    "long_context_cache_read_price_per_1k_tokens",
)
THRESHOLD = 272000
_MODEL_PATTERN = "%openai.gpt-5.6-%"
# 카드 배수 — long = short × mult.
_INPUT_MULT = "2"
_OUTPUT_MULT = "1.5"
_CACHE_MULT = "2"


def upgrade() -> None:
    # NULL 허용이라 백필 없음(PG11+ fast default 불필요, 재작성 없음).
    op.execute(
        f"ALTER TABLE model.model_pricings ADD COLUMN IF NOT EXISTS {THRESHOLD_COL} INTEGER"
    )
    for col in PRICE_COLS:
        op.execute(
            f"ALTER TABLE model.model_pricings ADD COLUMN IF NOT EXISTS {col} NUMERIC(10,6)"
        )

    # 값-가드 시딩: 열린 gpt-5.6 행의 현재 short 요율에 카드 배수를 곱해 long 을 채운다.
    op.execute(
        f"""
        UPDATE model.model_pricings p
        SET {THRESHOLD_COL} = {THRESHOLD},
            long_context_input_price_per_1k_tokens =
                p.input_price_per_1k_tokens * {_INPUT_MULT},
            long_context_output_price_per_1k_tokens =
                p.output_price_per_1k_tokens * {_OUTPUT_MULT},
            long_context_cache_creation_5m_price_per_1k_tokens =
                p.cache_creation_5m_price_per_1k_tokens * {_CACHE_MULT},
            long_context_cache_creation_1h_price_per_1k_tokens =
                p.cache_creation_1h_price_per_1k_tokens * {_CACHE_MULT},
            long_context_cache_read_price_per_1k_tokens =
                p.cache_read_price_per_1k_tokens * {_CACHE_MULT}
        FROM model.model_aliases a
        WHERE a.alias = p.model_alias
          AND p.effective_until IS NULL
          AND p.{THRESHOLD_COL} IS NULL
          AND a.provider_model_id LIKE '{_MODEL_PATTERN}'
        """
    )


def downgrade() -> None:
    # 이미지를 먼저 구버전으로 되돌린 뒤 컬럼을 지울 것. 컬럼과 함께 티어 데이터가 사라지고,
    # GPT-5.6 는 티어 이전(과소청구) 동작으로 복귀한다 — 스키마 호환 롤백 시 의도된 방향.
    for col in (*PRICE_COLS, THRESHOLD_COL):
        op.execute(f"ALTER TABLE model.model_pricings DROP COLUMN IF EXISTS {col}")
