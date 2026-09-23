# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""long-context 티어 (0038) 쓰기 경로 — 스키마 검증 + set_pricing 승계/제거 + create_model 왕복.

핵심 불변식: **short 단가만 고치는 PUT 이 272K 티어를 지우면 안 된다.** 옛 sync 설계의 실제
회귀가 이것이었다 — set_pricing 이 새 행을 short 5필드로만 만들어 long 컬럼을 매번 NULL 로
리셋했다. 아래 set_pricing 테스트가 그 회귀를 고정한다(생략=이전 행 승계, 명시 None=제거).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.core.cache_invalidation import CacheInvalidationManager
from app.models.model import ApiFormat, ModelAlias, ModelPricing, ModelStatus, Provider
from app.schemas.models import ModelCreateRequest, PricingRequest
from app.services.model_service import ModelService

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _svc() -> ModelService:
    return ModelService(cache_mgr=MagicMock(spec=CacheInvalidationManager))


def _model() -> ModelAlias:
    m = MagicMock(spec=ModelAlias)
    m.alias = "gpt-5.6-terra"
    m.provider = Provider.BEDROCK_RUNTIME_OPENAI
    m.provider_model_id = "us.openai.gpt-5.6-terra"
    m.endpoint_url = "https://bedrock-runtime.us-east-2.amazonaws.com/openai"
    m.api_format = ApiFormat.OPENAI_RESPONSES
    m.status = ModelStatus.ACTIVE
    m.description = None
    m.display_name = None
    m.allowed_clients = None
    m.created_at = _NOW
    m.updated_at = _NOW
    return m


def _tiered_prior() -> ModelPricing:
    p = MagicMock(spec=ModelPricing)
    p.input_price_per_1k_tokens = Decimal("0.0022")
    p.output_price_per_1k_tokens = Decimal("0.0132")
    p.cache_creation_5m_price_per_1k_tokens = Decimal("0.00275")
    p.cache_creation_1h_price_per_1k_tokens = Decimal("0.00275")
    p.cache_read_price_per_1k_tokens = Decimal("0.00022")
    p.long_context_threshold_tokens = 272000
    p.long_context_input_price_per_1k_tokens = Decimal("0.0044")
    p.long_context_output_price_per_1k_tokens = Decimal("0.0198")
    p.long_context_cache_creation_5m_price_per_1k_tokens = Decimal("0.0055")
    p.long_context_cache_creation_1h_price_per_1k_tokens = Decimal("0.0055")
    p.long_context_cache_read_price_per_1k_tokens = Decimal("0.00044")
    p.effective_from = _NOW
    p.effective_until = None
    return p


# ── 스키마 검증: 반쪽 설정 거부 (조용한 무과금 티어 방지) ──


def test_threshold_without_long_input_output_rejected():
    with pytest.raises(ValidationError):
        PricingRequest(
            input_price_per_1k_tokens=Decimal("0.0022"),
            output_price_per_1k_tokens=Decimal("0.0132"),
            long_context_threshold_tokens=272000,
            effective_from=_NOW,
        )


def test_long_prices_without_threshold_rejected():
    with pytest.raises(ValidationError):
        PricingRequest(
            input_price_per_1k_tokens=Decimal("0.0022"),
            output_price_per_1k_tokens=Decimal("0.0132"),
            long_context_input_price_per_1k_tokens=Decimal("0.0044"),
            long_context_output_price_per_1k_tokens=Decimal("0.0198"),
            effective_from=_NOW,
        )


def test_threshold_with_paid_short_cache_requires_long_cache():
    """short 캐시 단가가 0 보다 크면 그 버킷의 long 단가도 필수 — 캐시 슬라이스 과소청구 방지."""
    with pytest.raises(ValidationError):
        PricingRequest(
            input_price_per_1k_tokens=Decimal("0.0022"),
            output_price_per_1k_tokens=Decimal("0.0132"),
            cache_read_price_per_1k_tokens=Decimal("0.00022"),  # >0 → long cache_read 필수
            long_context_threshold_tokens=272000,
            long_context_input_price_per_1k_tokens=Decimal("0.0044"),
            long_context_output_price_per_1k_tokens=Decimal("0.0198"),
            effective_from=_NOW,
        )


def test_full_tier_accepted():
    req = PricingRequest(
        input_price_per_1k_tokens=Decimal("0.0022"),
        output_price_per_1k_tokens=Decimal("0.0132"),
        cache_read_price_per_1k_tokens=Decimal("0.00022"),
        cache_creation_5m_price_per_1k_tokens=Decimal("0.00275"),
        cache_creation_1h_price_per_1k_tokens=Decimal("0.00275"),
        long_context_threshold_tokens=272000,
        long_context_input_price_per_1k_tokens=Decimal("0.0044"),
        long_context_output_price_per_1k_tokens=Decimal("0.0198"),
        long_context_cache_read_price_per_1k_tokens=Decimal("0.00044"),
        long_context_cache_creation_5m_price_per_1k_tokens=Decimal("0.0055"),
        long_context_cache_creation_1h_price_per_1k_tokens=Decimal("0.0055"),
        effective_from=_NOW,
    )
    assert req.long_context_threshold_tokens == 272000


def test_no_tier_accepted():
    req = PricingRequest(
        input_price_per_1k_tokens=Decimal("0.003"),
        output_price_per_1k_tokens=Decimal("0.015"),
        effective_from=_NOW,
    )
    assert req.long_context_threshold_tokens is None


# ── set_pricing: 승계(생략) / 제거(명시 None) — NULL-wipe 회귀 가드 ──


async def test_short_only_put_inherits_prior_tier(mock_session, admin_user):
    """⚠️ 핵심 회귀 가드. long 필드를 생략한 단가 PUT 은 이전 행의 272K 티어를 승계해야 한다.

    (옛 sync/PUT 은 새 행을 short 5필드로만 만들어 티어를 매번 NULL 로 지웠다.)
    """
    svc = _svc()
    prior = _tiered_prior()
    data = PricingRequest(  # long 필드 전부 생략
        input_price_per_1k_tokens=Decimal("0.0025"),
        output_price_per_1k_tokens=Decimal("0.0140"),
        effective_from=_NOW,
    )
    with patch("app.services.model_service.fetch_bedrock_prices"), \
         patch("app.services.model_service.audit_logger") as mock_audit, \
         patch("app.services.model_service.ModelRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.get_by_alias = AsyncMock(return_value=_model())
        repo.get_current_pricing = AsyncMock(return_value=prior)
        repo.close_current_pricing = AsyncMock()
        repo.create_pricing = AsyncMock()
        mock_audit.log = AsyncMock()
        await svc.set_pricing(mock_session, alias="gpt-5.6-terra", data=data, actor=admin_user)

    written = repo.create_pricing.call_args.args[0]
    assert written.input_price_per_1k_tokens == Decimal("0.0025")  # short 는 갱신
    # long 티어는 이전 행에서 그대로 승계 — 지워지지 않았다
    assert written.long_context_threshold_tokens == 272000
    assert written.long_context_input_price_per_1k_tokens == Decimal("0.0044")
    assert written.long_context_cache_creation_1h_price_per_1k_tokens == Decimal("0.0055")


async def test_explicit_none_clears_prior_tier(mock_session, admin_user):
    """long 필드를 명시적으로 None 으로 보내면(model_fields_set 포함) 티어가 제거돼야 한다."""
    svc = _svc()
    prior = _tiered_prior()
    data = PricingRequest(
        input_price_per_1k_tokens=Decimal("0.0022"),
        output_price_per_1k_tokens=Decimal("0.0132"),
        long_context_threshold_tokens=None,  # 명시적 제거
        long_context_input_price_per_1k_tokens=None,
        long_context_output_price_per_1k_tokens=None,
        long_context_cache_creation_5m_price_per_1k_tokens=None,
        long_context_cache_creation_1h_price_per_1k_tokens=None,
        long_context_cache_read_price_per_1k_tokens=None,
        effective_from=_NOW,
    )
    with patch("app.services.model_service.audit_logger") as mock_audit, \
         patch("app.services.model_service.ModelRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.get_by_alias = AsyncMock(return_value=_model())
        repo.get_current_pricing = AsyncMock(return_value=prior)
        repo.close_current_pricing = AsyncMock()
        repo.create_pricing = AsyncMock()
        mock_audit.log = AsyncMock()
        await svc.set_pricing(mock_session, alias="gpt-5.6-terra", data=data, actor=admin_user)

    written = repo.create_pricing.call_args.args[0]
    assert written.long_context_threshold_tokens is None
    assert written.long_context_input_price_per_1k_tokens is None


async def test_create_model_persists_long_tier(mock_session, admin_user):
    svc = _svc()
    data = ModelCreateRequest(
        alias="gpt-5.6-terra",
        provider="BEDROCK_RUNTIME_OPENAI",
        provider_model_id="us.openai.gpt-5.6-terra",
        api_format="OPENAI_RESPONSES",
        input_price_per_1k_tokens=Decimal("0.0022"),
        output_price_per_1k_tokens=Decimal("0.0132"),
        long_context_threshold_tokens=272000,
        long_context_input_price_per_1k_tokens=Decimal("0.0044"),
        long_context_output_price_per_1k_tokens=Decimal("0.0198"),
    )
    with patch("app.services.model_service.audit_logger") as mock_audit, \
         patch("app.services.model_service.ModelRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.alias_exists_ci = AsyncMock(return_value=False)

        async def _ts(model):
            model.created_at = _NOW
            model.updated_at = _NOW

        repo.create_model = AsyncMock(side_effect=_ts)
        repo.create_pricing = AsyncMock()
        mock_audit.log = AsyncMock()
        await svc.create_model(mock_session, data=data, actor=admin_user)

    written = repo.create_pricing.call_args.args[0]
    assert written.long_context_threshold_tokens == 272000
    assert written.long_context_input_price_per_1k_tokens == Decimal("0.0044")
    assert written.long_context_output_price_per_1k_tokens == Decimal("0.0198")
