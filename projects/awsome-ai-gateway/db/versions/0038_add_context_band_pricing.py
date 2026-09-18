# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""컨텍스트 밴드 요금제: 프롬프트가 임계를 넘으면 요청 전체를 long 요율로 청구

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-18

## 무엇을 고치나 — 0025 가 이미 문서화한 구조적 과소청구

GPT-5.6 (terra/sol/luna) 모델카드는 요금표를 **크기축으로** 게시한다. OpenAI 문구를
AWS 카드가 그대로 인용한다:

    "Prompts with >272K input tokens are priced at 2x input and 1.5x output
     for the full request."

즉 한 요청의 프롬프트가 272,000 토큰을 넘으면 그 요청 **전체**가 long 요율로 재청구된다
— 초과분 누진이 아니라 계단 하나. 273K 와 999K 는 같은 배수. 카드 실측 배수:

    input x2.0,  cache(read/write) x2.0,  output x1.5

(terra short 2.20/2.75/0.22/13.20 → long 4.40/5.50/0.44/19.80, 정확히 위 배수. GovCloud
``long-ctx`` SKU 로 교차확인됨.)

그런데 ``model_pricings`` 는 alias 당 스칼라 요율 하나뿐, **크기축이 없었다.** 그래서
272K 초과 요청이 양 plane(Mantle ``codex-gpt-5.6-*`` + runtime ``gpt-5.6-*``)에서 절반
요율로 과소청구됐다. 0025 주석이 이 구멍을 이미 지적했지만("routine >272K prompts should
register a separate alias with the long rates") 미착수였다.

## ⚠️ Claude 는 밴드가 없다 — 이건 GPT-5.6 전용

Anthropic 4.6+ 는 1M 컨텍스트를 **표준 단가로** 청구한다(Bedrock SKU 중 Anthropic
long-ctx 0건). ``claude-*[1m]`` 이 비-1m 과 동일 단가인 것은 올바르다. 밴드를 켜는 대상은
``provider_model_id LIKE '%openai.gpt-5.6-%'`` 인 행뿐이다.

## 왜 배수인가 (독립 long 요율이 아니라)

카드가 게시하는 것은 규칙이다 — "2x input, 1.5x output". long 요율은 short 에서 파생된다.
독립 요율 5열을 더 저장하면 short 와 어긋날 수 있다(운영자가 한쪽만 바꾸면). 배수는
short 단일 진실원에서 파생되므로 변조 불가능하고, 카드의 규칙 형태와 동형이다. 미래에
비-배수 long 요율을 게시하는 모델이 나오면 그때 컬럼을 확장한다(YAGNI).

## 활성화 — 열린 행에 in-place UPDATE, 밴드 IS NULL 가드

⚠️ **0030/0033 의 "요율 행 UPDATE 금지·close 후 새 행" 규약은 여기 적용되지 않는다.**
그 규약의 목적은 과거 청구 기간의 요율을 재구성 가능하게 두는 것(요율을 덮어쓰면 과거
청구 근거가 사라진다)이다. 밴드 컬럼은 이 마이그레이션 이전에 **존재하지 않았으므로**
(NULL) 다른 값을 가졌던 과거 기간이 없다 — NULL→272000 은 어떤 청구 이력도 파괴하지
않는다. 게다가 ``model_pricings`` 에는 부분 유니크 ``(model_alias) WHERE effective_until
IS NULL`` (0034)가 있어 close 전에 새 열린 행을 넣으면 유니크 위반이다. 그래서 요율은
그대로 두고 밴드 컬럼만 현재 열린 행에 UPDATE 한다.

밴드는 미래 요청부터만 영향을 준다(usage_logs 는 불변, 소급 재청구 없음). 열린 행의
effective_from 이 과거여도 소급되지 않는 이유다.

가드는 ``long_context_threshold_tokens IS NULL`` — 아직 밴드가 안 켜진 행만 대상.
  * 재실행 안전: 켜진 행은 재실행 시 제외된다(멱등).
  * 운영자 안전: UI 로 다른 밴드를 이미 설정한 환경은 threshold 가 NULL 이 아니라 무시된다.
  * **요율 무관**: 밴드는 모델 속성이므로 운영자가 요율을 바꿨어도 그 현재 요율에 배수를
    얹는 것이 맞다 — exact-rate 매칭이 아니라 "밴드 미설정" 으로 게이트하는 이유다.

배수(2/2/1.5)와 임계(272000)는 모든 GPT-5.6 행에 동일하다 — 모델 계열의 공통 규칙이므로.
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_THRESHOLD = 272000
_INPUT_MULT = "2.0"
_CACHE_MULT = "2.0"
_OUTPUT_MULT = "1.5"
_MODEL_PATTERN = "%openai.gpt-5.6-%"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model.model_pricings "
        "ADD COLUMN IF NOT EXISTS long_context_threshold_tokens INTEGER"
    )
    for col in ("input", "cache", "output"):
        op.execute(
            f"ALTER TABLE model.model_pricings "
            f"ADD COLUMN IF NOT EXISTS long_context_{col}_mult NUMERIC(6,4) "
            f"NOT NULL DEFAULT 1"
        )

    # 현재 열린 GPT-5.6 행에 밴드를 켠다(요율은 그대로). 위 docstring 의 이유로
    # 새 행이 아니라 in-place UPDATE 다. 가드 ``threshold IS NULL`` = 멱등 + 운영자 존중.
    op.execute(
        f"""
        UPDATE model.model_pricings p
        SET long_context_threshold_tokens = {_THRESHOLD},
            long_context_input_mult  = {_INPUT_MULT},
            long_context_cache_mult   = {_CACHE_MULT},
            long_context_output_mult  = {_OUTPUT_MULT}
        FROM model.model_aliases a
        WHERE a.alias = p.model_alias
          AND p.effective_until IS NULL
          AND p.long_context_threshold_tokens IS NULL
          AND a.provider_model_id LIKE '{_MODEL_PATTERN}'
        """
    )


def downgrade() -> None:
    # ⚠️ 컬럼을 떨어뜨리면 밴드 데이터가 함께 사라진다. downgrade 후 재-upgrade 하면
    #    밴드가 다시 켜진다(멱등 가드가 처리). 닫아 둔 옛 행을 되살리지는 않는다 —
    #    되돌리면 GPT-5.6 는 밴드 이전(과소청구) 동작으로 복귀하고, 이는 스키마
    #    호환성 롤백 시 의도된 방향이다(long-context 만 영향, 금액 자체는 안전한 쪽).
    op.execute(
        "ALTER TABLE model.model_pricings DROP COLUMN IF EXISTS long_context_output_mult"
    )
    op.execute(
        "ALTER TABLE model.model_pricings DROP COLUMN IF EXISTS long_context_cache_mult"
    )
    op.execute(
        "ALTER TABLE model.model_pricings DROP COLUMN IF EXISTS long_context_input_mult"
    )
    op.execute(
        "ALTER TABLE model.model_pricings DROP COLUMN IF EXISTS long_context_threshold_tokens"
    )
