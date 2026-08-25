# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""add OPENMODEL sample alias 'llama-3-70b' (OpenAI-compatible)

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-25

첫 번째 **OPENMODEL** (자가호스팅 OpenAI-compatible) 카탈로그 항목을 등록한다.
지금까지 model_aliases 는 전부 provider='BEDROCK' / api_format='BEDROCK_NATIVE'
였다. 이 항목은 OPENMODEL 경로(provider='OPENMODEL', api_format='OPENAI_COMPATIBLE')
가 실제로 스키마·라우팅·가격 계층을 통과하는지 보여 주는 레퍼런스다.

    alias             = 'llama-3-70b'
    provider          = 'OPENMODEL'
    provider_model_id = 'meta-llama/Llama-3.1-70B-Instruct'
    endpoint_url      = NULL
    api_format        = 'OPENAI_COMPATIBLE'
    status            = 'ACTIVE'

**endpoint_url 은 의도적으로 NULL 이다.** OPENMODEL 엔드포인트는 배포 환경마다 다르므로
(사내 vLLM/TGI/Bedrock Marketplace 등) 여기서 특정 호스트를 박지 않는다. 실제 호출이
가능하려면 배포자가 endpoint_url 을 채우거나 model.routing_profiles 로 이 alias 를
가리켜야 한다. 그전까지는 카탈로그에만 존재하는 inert 항목이다(등록만으로 자동
노출되지 않는다 — team_allowed_models / user_allowed_models 게이팅은 0027 과 동일).

Bedrock alias 들과 달리 짧은 alias / full-ID 이중 등록을 하지 않는다. OPENMODEL 은
Bedrock inference-profile ID 체계를 쓰지 않으므로 alias 하나면 충분하다.

PRICING — in/out 모두 **0.000900 /1k** (= $0.90 /1M). Llama 3.1 70B 급 오픈모델의
일반적 자가호스팅 단가대다. OpenAI-compatible 오픈모델은 Bedrock Prompt Caching 이
없으므로 cache_5m/1h/read 는 전부 0(스키마 기본값)으로 둔다. 확정 단가가 생기면
model_pricings 는 effective_from 시계열이라 새 행을 넣어 정정한다(과거 계산 불변).

status 는 ACTIVE. INACTIVE 로 넣으면 router_service 가 ModelInactiveError 로 거부해
레퍼런스로서의 의미가 없다. endpoint_url 이 NULL 이라 실제 호출은 배포자가 라우팅을
붙이기 전까지 자연히 일어나지 않는다.

fresh-init DB 는 db/init/03_seed_data.sql 이 같은 alias 를 넣는다(ON CONFLICT DO
NOTHING). 이 마이그레이션은 이미 마이그레이션된 live DB 를 위한 것이며, 가격은
어차피 migration-owned 이다(0003 이 init 의 pricing 을 DROP+recreate 하므로 init
에서 가격을 넣어도 유실된다).
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

SYSTEM_USER = "00000000-0000-4000-a000-000000000010"
EFFECTIVE_FROM = "2026-08-25T00:00:00Z"

ALIAS = "llama-3-70b"
PROVIDER_MODEL_ID = "meta-llama/Llama-3.1-70B-Instruct"
DISPLAY_NAME = "Llama 3 70B (OpenModel)"
DESCRIPTION = "Meta Llama 3.1 70B Instruct — OpenAI-compatible OpenModel 레퍼런스 (endpoint 배포자 설정)"
PRICE_IN = "0.000900"
PRICE_OUT = "0.000900"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO model.model_aliases
            (alias, provider, provider_model_id, endpoint_url, api_format, status,
             description, display_name, created_by)
        VALUES
            ('{ALIAS}', 'OPENMODEL', '{PROVIDER_MODEL_ID}', NULL, 'OPENAI_COMPATIBLE', 'ACTIVE',
             '{DESCRIPTION}', '{DISPLAY_NAME}', '{SYSTEM_USER}')
        ON CONFLICT (alias) DO NOTHING
        """
    )

    # model_pricings PK 는 gen_random_uuid() 라 ON CONFLICT dedupe 불가.
    # 0025/0027 과 동일하게 (model_alias, effective_from) 로 가드한다 —
    # alias 만으로 가드하면 나중의 가격 정정 행이 막힌다.
    op.execute(
        f"""
        INSERT INTO model.model_pricings
            (id, model_alias, input_price_per_1k_tokens, output_price_per_1k_tokens,
             cache_creation_5m_price_per_1k_tokens, cache_creation_1h_price_per_1k_tokens,
             cache_read_price_per_1k_tokens, effective_from, created_by)
        SELECT gen_random_uuid(), '{ALIAS}',
               {PRICE_IN}, {PRICE_OUT}, 0, 0, 0,
               '{EFFECTIVE_FROM}', '{SYSTEM_USER}'
        WHERE NOT EXISTS (
            SELECT 1 FROM model.model_pricings
             WHERE model_alias = '{ALIAS}' AND effective_from = '{EFFECTIVE_FROM}'
        )
        """
    )


def downgrade() -> None:
    """alias 를 참조하는 자식 행까지 children-before-parent 순으로 제거(0027 과 동일).

    model_aliases.alias 를 참조하는 FK 는 전부 ON DELETE NO ACTION 이라, 운영자가
    이 모델을 팀/사용자에 허용하거나 라우팅을 붙인 뒤 downgrade 하면
    ForeignKeyViolationError 로 죽는다. 그래서 자식부터 지운다.
    """
    aliases = f"'{ALIAS}'"

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
