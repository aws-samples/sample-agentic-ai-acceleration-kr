# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""컨텍스트 밴드 요금제 — 프롬프트가 임계를 넘으면 요청 전체가 long 요율.

무엇을 고치나
-------------
GPT-5.6 모델카드는 요금표를 크기축으로 게시한다. OpenAI 문구를 AWS 카드가 그대로 인용:
"Prompts with >272K input tokens are priced at 2x input and 1.5x output for the full
request." 즉 한 요청 프롬프트가 272,000 을 넘으면 그 요청 **전체**가 long 요율(계단 하나,
초과분 누진 아님). 카드 실측 배수: input×2, cache×2, output×1.5.

그런데 ``model_pricings`` 에는 크기축이 없어서 272K 초과 요청이 절반 요율로
**과소청구**됐다(0025 주석이 지적, 미착수였다). 이 변경이 밴드를 넣는다.

⚠️ Claude 는 밴드가 없다 — 1M 컨텍스트를 표준 단가로 청구한다. threshold=None 이면
   밴드 없음이 오늘 동작이고, 이 파일의 대조군이 그것을 고정한다.

두 과금점
---------
  * ``calculate_cost``   — 실비(usage_logs.cost_usd → 예산 차감 → 다음 요청 429 판정)
  * ``_estimate_cost``   — 예약 추정(사전 차감). long 요청이 밴드를 안 타면 실비의 절반만
                           예약해 조기 429 방어가 무너진다.

임계와 비교하는 값은 **프롬프트 전체 토큰** = 세 입력 버킷의 합(순입력+캐시읽기+캐시쓰기).
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal

import pytest

from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    TokenUsage,
)
from app.services.cost_recorder import calculate_cost
from app.services.rate_limit_enforcement import _estimate_cost

# terra Geo CRIS short 요율 (per 1k) — 카드값.
_IN = Decimal("0.0022")
_OUT = Decimal("0.0132")
_CW = Decimal("0.00275")
_CR = Decimal("0.00022")
_THRESHOLD = 272000


def _cfg(*, banded: bool, threshold: int = _THRESHOLD) -> ModelConfigSchema:
    p = ModelPricingSchema(
        input_per_1k=_IN,
        output_per_1k=_OUT,
        cache_write_per_1k=_CW,
        cache_read_per_1k=_CR,
    )
    if banded:
        p.long_context_threshold_tokens = threshold
        p.long_context_input_mult = Decimal("2.0")
        p.long_context_cache_mult = Decimal("2.0")
        p.long_context_output_mult = Decimal("1.5")
    return ModelConfigSchema(
        provider_model_id="us.openai.gpt-5.6-terra",
        alias="gpt-5.6-terra",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.ANTHROPIC_MESSAGES,
        pricing=p,
        status=ModelStatus.ACTIVE,
        created_at=datetime(2026, 1, 1),
    )


def _usage(*, inp=0, out=0, cw=0, cr=0) -> TokenUsage:
    u = TokenUsage(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_input_tokens=cw,
        cache_read_input_tokens=cr,
    )
    u.total_tokens = inp + out
    return u


# ─────────────────────────────────────────────────────────────────────────────
# 1. calculate_cost — 실비
# ─────────────────────────────────────────────────────────────────────────────


def test_below_threshold_uses_the_short_rate():
    cost = calculate_cost(_usage(inp=100_000, out=500), _cfg(banded=True))
    # 100k × 0.0022 + 0.5k × 0.0132 = 0.22 + 0.0066
    assert cost == Decimal("0.226600"), cost


def test_above_threshold_rerates_the_whole_request_at_long():
    """⚠️ 이 파일의 핵심. 300K > 272K → 전체 요청 long 요율."""
    cost = calculate_cost(_usage(inp=300_000, out=500), _cfg(banded=True))
    # input 300k × 0.0022 × 2 = 1.32 ; output 0.5k × 0.0132 × 1.5 = 0.0099
    assert cost == Decimal("1.329900"), cost


def test_the_boundary_is_strictly_greater_than():
    """정확히 272,000 은 short, 272,001 은 long — 계단의 위치."""
    at = calculate_cost(_usage(inp=272_000, out=0), _cfg(banded=True))
    over = calculate_cost(_usage(inp=272_001, out=0), _cfg(banded=True))
    # at: 272 × 0.0022 = 0.5984 (short) ; over: long 이므로 ×2 ≈ 1.19680
    assert at == Decimal("0.598400"), at
    assert over > at * Decimal("1.9"), (at, over)  # 사실상 2배(반올림 여유)


def test_the_band_trigger_counts_the_whole_prompt_not_just_non_cached():
    """⚠️ 임계 비교는 세 입력 버킷의 합이다.

    순입력 100K 만 보면 밴드 미달이지만, 캐시읽기 180K 를 더하면 프롬프트 280K > 272K.
    캐시 토큰도 프롬프트의 일부다(OpenAI 의 ">272K input tokens" 은 캐시 포함).
    """
    u = _usage(inp=100_000, cr=180_000, out=0)
    cost = calculate_cost(u, _cfg(banded=True))
    # long: input 100k×0.0022×2 = 0.44 ; cache_read 180k×0.00022×2 = 0.0792
    assert cost == Decimal("0.519200"), cost
    # 대조군: 같은 토큰인데 순입력만 세면(밴드 미발동) 절반 요율일 것 — 그렇지 않음을 확인
    short = calculate_cost(u, _cfg(banded=False))
    assert cost == short * 2, (cost, short)


def test_a_model_without_a_band_never_rerates():
    """⚠️ Claude 대조군. threshold=None → 300K 여도 short. 1M 을 표준 단가로 청구한다."""
    cost = calculate_cost(_usage(inp=300_000, out=500), _cfg(banded=False))
    assert cost == Decimal("0.666600"), cost  # 300×0.0022 + 0.5×0.0132, 배수 없음


def test_cache_write_is_also_rerated_at_the_cache_multiplier():
    u = _usage(inp=280_000, cw=5_000, out=0)
    cost = calculate_cost(u, _cfg(banded=True))
    # long: input 280k×0.0022×2 = 1.232 ; cache_write 5k×0.00275×2 = 0.0275
    assert cost == Decimal("1.259500"), cost


# ─────────────────────────────────────────────────────────────────────────────
# 2. _estimate_cost — 예약
# ─────────────────────────────────────────────────────────────────────────────


def test_reservation_estimate_bands_a_long_request():
    """⚠️ 예약이 밴드를 안 타면 long 요청이 실비의 절반만 예약 → 조기 429 무너짐."""
    est = _estimate_cost(_cfg(banded=True), estimated_input=300_000, max_output=1000)
    # input 300k×0.0022×2 = 1.32 ; output 1k×0.0132×1.5 = 0.0198
    assert est == Decimal("1.339800"), est


def test_reservation_estimate_short_below_threshold():
    est = _estimate_cost(_cfg(banded=True), estimated_input=100_000, max_output=1000)
    assert est == Decimal("0.233200"), est  # 100×0.0022 + 1×0.0132


def test_reservation_estimate_no_band_model():
    """⚠️ 대조군. 밴드 없는 모델은 300K 여도 short 예약."""
    est = _estimate_cost(_cfg(banded=False), estimated_input=300_000, max_output=1000)
    assert est == Decimal("0.673200"), est  # 300×0.0022 + 1×0.0132


# ─────────────────────────────────────────────────────────────────────────────
# 3. 스키마 기본값 + 배선
# ─────────────────────────────────────────────────────────────────────────────


def test_schema_defaults_to_no_band():
    """기본값이 밴드 없음이어야 한다 — 시드/구버전 행이 밴드를 우연히 켜면 안 된다."""
    p = ModelPricingSchema(input_per_1k=Decimal("0.001"), output_per_1k=Decimal("0.002"))
    assert p.long_context_threshold_tokens is None
    assert p.long_context_input_mult == Decimal("1")
    assert p.long_context_cache_mult == Decimal("1")
    assert p.long_context_output_mult == Decimal("1")


def test_orm_to_schema_carries_the_band_fields():
    """⚠️ ORM 행 → ModelConfigSchema 로 밴드가 흐르지 않으면 DB 에 켜도 청구가 안 바뀐다.

    다른 요율 컬럼처럼 직접 읽는다(셋 다 NOT NULL DEFAULT 1). 이 배선이 끊기면
    allowed_clients 사고처럼 마이그레이션·게이트가 다 있어도 청구가 안 바뀐다.
    """
    from app.services.router_service import _orm_to_schema

    class _Alias:
        provider_model_id = "us.openai.gpt-5.6-terra"
        alias = "gpt-5.6-terra"
        provider = "BEDROCK"
        api_format = "ANTHROPIC_MESSAGES"
        endpoint_url = ""
        status = "ACTIVE"
        created_at = datetime(2026, 1, 1)
        description = None
        allowed_clients = None

    class _Row:
        input_price_per_1k_tokens = _IN
        output_price_per_1k_tokens = _OUT
        cache_creation_5m_price_per_1k_tokens = _CW
        cache_creation_1h_price_per_1k_tokens = Decimal("0")
        cache_read_price_per_1k_tokens = _CR
        long_context_threshold_tokens = _THRESHOLD
        long_context_input_mult = Decimal("2.0")
        long_context_cache_mult = Decimal("2.0")
        long_context_output_mult = Decimal("1.5")

    cfg = _orm_to_schema(_Alias(), _Row())
    assert cfg.pricing.long_context_threshold_tokens == _THRESHOLD
    # 실제로 청구가 밴드를 타는지
    assert calculate_cost(_usage(inp=300_000, out=0), cfg) == Decimal("1.320000")


def test_the_band_survives_redis_cache_round_trip():
    """ModelConfigSchema 는 Redis 에 model_dump_json 으로 캐시된다 — 밴드가 왕복해야 한다."""
    cfg = _cfg(banded=True)
    restored = ModelConfigSchema.model_validate_json(cfg.model_dump_json())
    assert restored.pricing.long_context_threshold_tokens == _THRESHOLD
    assert calculate_cost(_usage(inp=300_000, out=0), restored) == Decimal("1.320000")


# ─────────────────────────────────────────────────────────────────────────────
# 4. 실 PostgreSQL — 마이그레이션한 행이 청구까지 흐르는지
# ─────────────────────────────────────────────────────────────────────────────
#
# ⚠️ ORM 컬럼명과 DB 컬럼명이 어긋나면 값이 조용히 흐르지 않는다(이 저장소의
#    allowed_clients 사고가 정확히 그랬다 — 마이그레이션·게이트는 다 있는데 ORM 만
#    컬럼을 몰라 게이트가 무력화). 실 행을 ORM 으로 읽어 청구까지 태워 그 드리프트를 막는다.

_pg = pytest.mark.skipif(
    not os.environ.get("PROOF_DSN"),
    reason="PROOF_DSN 미설정 — 실 PG 필요(마이그레이션 0038 적용된 head DB).",
)


@_pg
async def test_migrated_gpt56_row_bills_long_context_through_the_orm():
    """0038 이 켠 밴드가 ORM → ModelConfigSchema → calculate_cost 까지 흐른다."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.model import ModelAlias, ModelPricing
    from app.services.router_service import _orm_to_schema

    engine = create_async_engine(os.environ["PROOF_DSN"])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            alias_row = (
                await db.execute(
                    select(ModelAlias).where(ModelAlias.alias == "gpt-5.6-terra")
                )
            ).scalar_one_or_none()
            if alias_row is None:
                pytest.skip("gpt-5.6-terra 별칭 없음 — 마이그레이션 0025/0032 미적용 DB")
            pricing_row = (
                await db.execute(
                    select(ModelPricing)
                    .where(ModelPricing.model_alias == "gpt-5.6-terra")
                    .where(ModelPricing.effective_until.is_(None))
                )
            ).scalar_one()

        # 마이그레이션이 실제로 켰는지 — ORM 이 읽은 값으로 확인(컬럼명 드리프트 방어).
        assert pricing_row.long_context_threshold_tokens == 272000
        assert pricing_row.long_context_input_mult == Decimal("2.0000")
        assert pricing_row.long_context_output_mult == Decimal("1.5000")

        cfg = _orm_to_schema(alias_row, pricing_row)
        # 300K > 272K → long. short(밴드 무시)의 정확히 배수여야 한다.
        long_cost = calculate_cost(_usage(inp=300_000, out=1000), cfg)
        short_input = (Decimal(300_000) / 1000) * cfg.pricing.input_per_1k
        short_output = (Decimal(1000) / 1000) * cfg.pricing.output_per_1k
        expected = (short_input * 2 + short_output * Decimal("1.5")).quantize(Decimal("0.000001"))
        assert long_cost == expected, (long_cost, expected)

        # 밴드 미달(200K)은 short.
        assert calculate_cost(_usage(inp=200_000, out=0), cfg) == (
            (Decimal(200_000) / 1000) * cfg.pricing.input_per_1k
        ).quantize(Decimal("0.000001"))
    finally:
        await engine.dispose()


@_pg
async def test_a_claude_row_has_no_band_in_the_migrated_db():
    """⚠️ 대조군(실 PG). GPT-5.6 외 모델은 밴드가 NULL 이어야 한다.

    마이그레이션이 LIKE 패턴을 잘못 넓혀 Claude 까지 밴드를 켜면 1M 요청이 2배로
    과대청구된다 — 반대 방향의 금액 오류.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(os.environ["PROOF_DSN"])
    try:
        async with engine.connect() as c:
            banded_non_gpt = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM model.model_pricings p "
                        "JOIN model.model_aliases a ON a.alias = p.model_alias "
                        "WHERE p.effective_until IS NULL "
                        "AND p.long_context_threshold_tokens IS NOT NULL "
                        "AND a.provider_model_id NOT LIKE '%openai.gpt-5.6-%'"
                    )
                )
            ).scalar_one()
        assert banded_non_gpt == 0, f"GPT-5.6 외 {banded_non_gpt}개 행에 밴드가 켜졌다"
    finally:
        await engine.dispose()
