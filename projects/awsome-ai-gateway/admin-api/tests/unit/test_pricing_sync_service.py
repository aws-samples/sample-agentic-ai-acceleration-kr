# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""PricingSyncService 정규화 로직 검증 — 가짜 pricing client(실제 AWS 호출 없음).

핵심 위험 지점: Price List SKU → per-1k 단위 정규화 + input/output/cache 분류 +
캐시 단가 파생(미게시 시). 실 AWS 호출은 통합테스트 영역이므로 여기선 결정적 로직만.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.services.pricing_sync_service import PricingSyncService


class _FakePricingClient:
    """boto3 pricing client 흉내 — get_products 가 PriceList(JSON 문자열 배열) 반환."""

    def __init__(self, products: list[dict]):
        self._products = [json.dumps(p) for p in products]

    def get_products(self, **kwargs):
        return {"PriceList": self._products, "NextToken": None}


def _product(model_id: str, usagetype: str, usd: str, unit: str) -> dict:
    return {
        "product": {"attributes": {"model": model_id, "usagetype": usagetype}},
        "terms": {
            "OnDemand": {
                "x": {"priceDimensions": {"y": {"pricePerUnit": {"USD": usd}, "unit": unit}}}
            }
        },
    }


@pytest.mark.asyncio
async def test_normalizes_per_1m_to_per_1k():
    """per-1M tokens 단가를 per-1k 로 환산(÷1000)."""
    client = _FakePricingClient([
        _product("anthropic.claude-x", "InputTokenCount", "3.00", "1M tokens"),   # $3/1M → $0.003/1k
        _product("anthropic.claude-x", "OutputTokenCount", "15.00", "1M tokens"),  # $15/1M → $0.015/1k
    ])
    res = await PricingSyncService(client).fetch_bedrock_prices()
    assert not res.errors
    p = res.prices["anthropic.claude-x"]
    assert p.input_per_1k == Decimal("0.003")
    assert p.output_per_1k == Decimal("0.015")


@pytest.mark.asyncio
async def test_cache_derived_when_not_published():
    """캐시 SKU 가 없으면 input 기반 파생(5m=×1.25, 1h=×2.0, read=×0.1) + derived 플래그."""
    client = _FakePricingClient([
        _product("m1", "InputTokenCount", "1.00", "1K tokens"),   # $1/1k
        _product("m1", "OutputTokenCount", "2.00", "1K tokens"),
    ])
    res = await PricingSyncService(client).fetch_bedrock_prices()
    p = res.prices["m1"]
    assert p.cache_derived is True
    assert p.cache_5m_per_1k == Decimal("1.25")   # 1.00 × 1.25
    assert p.cache_1h_per_1k == Decimal("2.0")    # 1.00 × 2.0
    assert p.cache_read_per_1k == Decimal("0.1")  # 1.00 × 0.1


@pytest.mark.asyncio
async def test_cache_explicit_when_published():
    """캐시 SKU 가 있으면 파생 안 하고 그 값 사용, derived=False."""
    client = _FakePricingClient([
        _product("m2", "InputTokenCount", "1.00", "1K tokens"),
        _product("m2", "OutputTokenCount", "2.00", "1K tokens"),
        _product("m2", "CacheWriteInputTokenCount", "0.30", "1K tokens"),
        _product("m2", "CacheRead-InputTokenCount", "0.05", "1K tokens"),
        _product("m2", "CacheWrite-1h-InputTokenCount", "0.60", "1K tokens"),
    ])
    res = await PricingSyncService(client).fetch_bedrock_prices()
    p = res.prices["m2"]
    assert p.cache_derived is False
    assert p.cache_5m_per_1k == Decimal("0.3")
    assert p.cache_read_per_1k == Decimal("0.05")
    assert p.cache_1h_per_1k == Decimal("0.6")


@pytest.mark.asyncio
async def test_skips_partial_models_without_input_output():
    """input/output 둘 다 없으면 비용계산 불가 → 스킵(부분 데이터)."""
    client = _FakePricingClient([
        _product("partial", "CacheReadInputTokenCount", "0.05", "1K tokens"),  # cache만
    ])
    res = await PricingSyncService(client).fetch_bedrock_prices()
    assert "partial" not in res.prices


@pytest.mark.asyncio
async def test_fetch_failure_is_fail_soft():
    """get_products 예외 → errors 에 담고 빈 결과(fail-soft, hot-path 안 죽임)."""

    class _Boom:
        def get_products(self, **kwargs):
            raise RuntimeError("AccessDenied")

    res = await PricingSyncService(_Boom()).fetch_bedrock_prices()
    assert res.prices == {}
    assert res.errors and "AccessDenied" in res.errors[0]
