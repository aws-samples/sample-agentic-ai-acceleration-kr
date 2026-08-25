# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add Claude Opus 5 / Sonnet 5 aliases (global inference profiles)

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-19

Opus 5 / Sonnet 5 를 model registry 에 등록. alias 는 0006(Opus 4.8) 패턴을 그대로
따라 짧은 alias 와 full inference-profile ID 를 **둘 다** 넣는다 — 클라이언트가 어느
형태로 보내든 라우팅되도록(claude-code 는 짧은 alias, 일부 SDK 는 full ID 를 보낸다).

등록 근거는 전부 이 계정에서의 실측이다(2026-08-19, ap-northeast-2):

    bedrock get-foundation-model
      anthropic.claude-opus-5    streaming=True  inferenceTypes=[INFERENCE_PROFILE]
      anthropic.claude-sonnet-5  streaming=True  inferenceTypes=[INFERENCE_PROFILE]
    bedrock-runtime invoke_model (global.anthropic.claude-opus-5 / -sonnet-5)
      → 200, usage{input_tokens:9, output_tokens:4}, text='OK'
    anthropic_beta=["context-1m-2025-08-07"] 도 200 (1M 컨텍스트 수용)

``inferenceTypesSupported`` 가 INFERENCE_PROFILE **뿐**이라 foundation-model ID 로는
호출되지 않는다. 그래서 provider_model_id 는 반드시 ``global.`` 프리픽스가 붙은
inference profile ID 여야 한다(4.7/4.8 과 동일한 제약).

⚠️ **Fable 5 는 의도적으로 제외한다.** global.anthropic.claude-fable-5 는 프로파일
목록에는 있지만 ap-northeast-2 에서 호출이 거부된다:

    ValidationException: data retention mode 'default' is not available for this model

같은 호출이 us-east-1 / us-east-2 에서는 200 이다. 즉 모델은 살아 있고 우리 리전
(routing_profiles.claude-code.region = ap-northeast-2)에서만 막힌 것이다. 여기서
alias 를 등록하면 UI 목록엔 뜨지만 실제 호출이 전부 실패하는 "죽은 선택지"가 되므로,
리전 정책이 풀리거나 별도 리전 라우팅을 붙일 때 별도 마이그레이션으로 추가한다.

PRICING — **공시 단가가 아직 없다.** 두 경로로 확인했다:
  * AWS Price List API(GetProducts, serviceCode=AmazonBedrock) 전수 11,008개 스캔 →
    Claude 5 계열 SKU 0건. Price List 에 있는 Anthropic 모델은 Claude 2.0/2.1/
    3 Haiku/3 Sonnet/Instant 뿐이다(= admin-api 의 PricingSyncService 로도 못 가져온다).
  * aws.amazon.com/bedrock/pricing — Opus 5/Sonnet 5 미기재.

그래서 0006(Opus 4.8)이 세운 선례를 그대로 따른다: **직전 세대 동일 단가로 등록하고,
확정 시 별도 마이그레이션으로 정정**한다. 임의 추정가는 청구 오류를 유발하므로 절대
넣지 않는다.

    Opus 5    ← Opus 4.8/4.7 과 동일   in 0.005000 / out 0.025000 / 5m 0.006250 /
                                       1h 0.010000 / read 0.000500
    Sonnet 5  ← Sonnet 4.6 과 동일     in 0.003000 / out 0.015000 / 5m 0.003750 /
                                       1h 0.006000 / read 0.000300

Sonnet 4.6 행에는 5m/1h/read 가 각각 0.003750/0.006000/0.000300 으로 들어가 있는데
이는 seed 의 파생 산식(5m=in×1.25, 1h=in×2.0, read=in×0.1)과 일치한다. 같은 산식을
Sonnet 5 에 적용하면 동일한 값이 나오므로 그대로 쓴다.

⚠️ 단가가 잠정값이라는 사실은 **비용 대시보드에 그대로 반영된다**. Claude 5 사용량이
붙기 시작하면 cost_usd 는 "4.8 단가로 계산된 값"이다. 공시 단가가 4.8 과 다르면 그
기간의 비용은 소급 정정이 필요하다(model_pricings 는 effective_from 시계열이라
새 행을 넣어도 과거 행의 계산 결과는 바뀌지 않는다).

status 는 ACTIVE 로 넣는다. 실호출이 200 으로 확인된 모델이고, INACTIVE 로 넣으면
router_service 가 ModelInactiveError 로 거부해 등록의 의미가 없다.

default_model 은 바꾸지 않는다. routing_profiles.claude-code.default_model 은 NULL
(클라이언트가 보낸 모델을 그대로 쓰는 pass-through)이라 여기 손댈 것이 없고, 사용자가
Opus 5 를 쓰려면 요청에 그 alias 를 넣으면 된다. 팀/사용자 허용목록(team_allowed_models
/ user_allowed_models)이 설정된 조직은 **그 목록에 새 alias 를 추가해야** 보인다 —
등록만으로 자동 노출되지 않는다(의도된 동작: 신모델 자동 개방 방지).
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"
EFFECTIVE_FROM = "2026-08-19T00:00:00Z"

# (short_alias, profile_id, display_name, description,
#  in/1k, cache_5m/1k, cache_1h/1k, cache_read/1k, out/1k)
MODELS = [
    (
        "claude-opus-5",
        "global.anthropic.claude-opus-5",
        "Claude Opus 5",
        "Claude Opus 5 (Global inference profile) — 단가 잠정(Opus 4.8 동일), 공시 시 정정",
        "0.005000", "0.006250", "0.010000", "0.000500", "0.025000",
    ),
    (
        "claude-sonnet-5",
        "global.anthropic.claude-sonnet-5",
        "Claude Sonnet 5",
        "Claude Sonnet 5 (Global inference profile) — 단가 잠정(Sonnet 4.6 동일), 공시 시 정정",
        "0.003000", "0.003750", "0.006000", "0.000300", "0.015000",
    ),
]


def upgrade() -> None:
    for short, profile, display, desc, p_in, p_5m, p_1h, p_read, p_out in MODELS:
        # 짧은 alias + full profile ID 둘 다 등록(0006 패턴). 둘 다 같은
        # provider_model_id 를 가리키므로 어느 형태로 와도 동일 모델로 라우팅된다.
        for alias in (short, profile):
            suffix = "" if alias == short else " (full ID)"
            op.execute(
                f"""
                INSERT INTO model.model_aliases
                    (alias, provider, provider_model_id, endpoint_url, api_format, status,
                     description, display_name, created_by)
                VALUES
                    ('{alias}', 'BEDROCK', '{profile}', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
                     '{desc}{suffix}', '{display}', '{SYSTEM_USER}')
                ON CONFLICT (alias) DO NOTHING
                """
            )

            # model_pricings PK 는 gen_random_uuid() 라 ON CONFLICT 로 dedupe 가
            # 불가능하다. 0025 와 같이 (model_alias, effective_from) 로 가드한다 —
            # alias 만으로 가드하면 나중의 가격 정정 행이 막힌다.
            op.execute(
                f"""
                INSERT INTO model.model_pricings
                    (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
                     cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
                     cache_read_price_per_1k_tokens, effective_from, created_by)
                SELECT gen_random_uuid(), '{alias}',
                       {p_in}, {p_out}, {p_5m}, {p_1h}, {p_read},
                       '{EFFECTIVE_FROM}', '{SYSTEM_USER}'
                WHERE NOT EXISTS (
                    SELECT 1 FROM model.model_pricings
                     WHERE model_alias = '{alias}' AND effective_from = '{EFFECTIVE_FROM}'
                )
                """
            )


def downgrade() -> None:
    """alias 를 참조하는 자식 행까지 children-before-parent 순으로 제거.

    0025 가 실측으로 확인한 것과 동일한 제약이다: model_aliases.alias 를 참조하는
    FK 6개가 **모두 ON DELETE NO ACTION** 이라, 운영자가 이 모델을 팀/사용자에게
    허용(= 정상적인 사용 개시 절차)한 뒤 downgrade 하면 ForeignKeyViolationError 로
    죽고, env.py 의 transaction_per_migration 때문에 DB 가 이 리비전에 갇힌다.

        model.model_pricings      .model_alias
        model.team_allowed_models .model_alias
        model.user_allowed_models .model_alias
        model.rate_limit_configs  .model_alias
        budget.downgrade_policies .from_model_alias
        budget.downgrade_policies .to_model_alias

    routing_profiles.default_model 은 FK 가 없는 평범한 컬럼이라 DELETE 로는 정리되지
    않는다. 이 마이그레이션은 default_model 을 바꾸지 않지만, 운영자가 수동으로
    Claude 5 를 기본값으로 올려둔 뒤 downgrade 하는 경우 dangling 기본값이 남아
    해당 클라이언트의 모든 요청이 404 가 된다. 그래서 방어적으로 NULL 로 되돌린다
    (claude-code 프로파일의 원래 값이 NULL = pass-through).
    """
    aliases = ", ".join(f"'{a}'" for m in MODELS for a in (m[0], m[1]))

    op.execute(
        f"""
        UPDATE model.routing_profiles
           SET default_model = NULL
         WHERE default_model IN ({aliases})
        """
    )

    for table, column in (
        ("model.model_pricings", "model_alias"),
        ("model.team_allowed_models", "model_alias"),
        ("model.user_allowed_models", "model_alias"),
        ("model.rate_limit_configs", "model_alias"),
        ("budget.downgrade_policies", "from_model_alias"),
        ("budget.downgrade_policies", "to_model_alias"),
    ):
        op.execute(f"DELETE FROM {table} WHERE {column} IN ({aliases})")

    op.execute(f"DELETE FROM model.model_aliases WHERE alias IN ({aliases})")
