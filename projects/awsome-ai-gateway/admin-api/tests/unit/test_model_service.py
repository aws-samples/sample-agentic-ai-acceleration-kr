# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import ConflictError, NotFoundError
from app.models.model import ApiFormat, ModelAlias, ModelPricing, ModelStatus, Provider
from app.schemas.models import ModelCreateRequest, ModelUpdateRequest, PricingRequest, StatusPatchRequest
from app.schemas.common import ApiFormatEnum, ProviderEnum
from app.services.model_service import ModelService


@pytest.fixture
def model_service(cache_mgr: CacheInvalidationManager) -> ModelService:
    return ModelService(cache_mgr=cache_mgr)


def _make_model(alias: str = "claude-sonnet") -> ModelAlias:
    m = MagicMock(spec=ModelAlias)
    m.alias = alias
    m.provider = Provider.BEDROCK
    m.provider_model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    m.endpoint_url = None
    m.api_format = ApiFormat.BEDROCK_NATIVE
    m.status = ModelStatus.ACTIVE
    m.description = "test model"
    m.display_name = None  # _to_response reads display_name; MagicMock would yield a non-str → pydantic error
    m.created_at = datetime.now(timezone.utc)
    m.updated_at = datetime.now(timezone.utc)
    return m


def _make_pricing(alias: str = "claude-sonnet") -> ModelPricing:
    p = MagicMock(spec=ModelPricing)
    p.input_price_per_1k_tokens = Decimal("0.003")
    p.output_price_per_1k_tokens = Decimal("0.015")
    p.cache_creation_5m_price_per_1k_tokens = Decimal("0.00375")
    p.cache_creation_1h_price_per_1k_tokens = Decimal("0.006")
    p.cache_read_price_per_1k_tokens = Decimal("0.0003")
    p.effective_from = datetime.now(timezone.utc)
    p.effective_until = None
    return p


class TestCreateModel:
    async def test_create_model_success(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = ModelCreateRequest(
            alias="claude-sonnet",
            provider=ProviderEnum.BEDROCK,
            provider_model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
            api_format=ApiFormatEnum.BEDROCK_NATIVE,
            input_price_per_1k_tokens=Decimal("0.003"),
            output_price_per_1k_tokens=Decimal("0.015"),
        )

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.alias_exists_ci = AsyncMock(return_value=False)

            # Simulate DB-default timestamps that a real INSERT flush would set,
            # so _to_response (ModelResponse) validates created_at/updated_at.
            async def _set_timestamps(model):
                model.created_at = datetime.now(timezone.utc)
                model.updated_at = datetime.now(timezone.utc)

            repo.create_model = AsyncMock(side_effect=_set_timestamps)
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await model_service.create_model(mock_session, data=data, actor=admin_user)

        assert result.alias == "claude-sonnet"
        assert result.provider == Provider.BEDROCK
        # P0-④: create_model is now invalidate-only (DEL model:{alias} + model:list);
        # it must NOT pre-seed a (previously flat, TTL-less) model cache entry.
        # The gateway populates model:{alias} with the correct nested shape + TTL
        # on first cache-miss. So we assert DEL happened and SET did not.
        assert mock_redis.delete.call_count >= 1
        mock_redis.set.assert_not_called()

    async def test_create_model_duplicate_alias_raises(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = ModelCreateRequest(
            alias="claude-sonnet",
            provider=ProviderEnum.BEDROCK,
            provider_model_id="test",
            api_format=ApiFormatEnum.BEDROCK_NATIVE,
            input_price_per_1k_tokens=Decimal("0.003"),
            output_price_per_1k_tokens=Decimal("0.015"),
        )

        with patch("app.services.model_service.ModelRepository") as MockRepo:
            MockRepo.return_value.alias_exists_ci = AsyncMock(return_value=True)

            with pytest.raises(ConflictError):
                await model_service.create_model(mock_session, data=data, actor=admin_user)


class TestUpdateModel:
    async def test_update_model_not_found(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = ModelUpdateRequest(description="new desc")

        with patch("app.services.model_service.ModelRepository") as MockRepo:
            MockRepo.return_value.update_model = AsyncMock(return_value=None)

            with pytest.raises(NotFoundError):
                await model_service.update_model(mock_session, alias="missing", data=data, actor=admin_user)

    async def test_update_model_invalidates_cache(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = ModelUpdateRequest(description="updated")
        model = _make_model()
        pricing = _make_pricing()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.update_model = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=pricing)
            mock_audit.log = AsyncMock()

            await model_service.update_model(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        # Cache invalidation called
        assert mock_redis.delete.call_count >= 1


class TestSetPricing:
    async def test_set_pricing_preserves_history(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = PricingRequest(
            input_price_per_1k_tokens=Decimal("0.005"),
            output_price_per_1k_tokens=Decimal("0.025"),
            effective_from=datetime.now(timezone.utc),
        )
        model = _make_model()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.get_by_alias = AsyncMock(return_value=model)
            repo.close_current_pricing = AsyncMock()
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            await model_service.set_pricing(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        repo.close_current_pricing.assert_called_once()
        repo.create_pricing.assert_called_once()

    async def test_set_pricing_persists_cache_prices(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """Bedrock Prompt Caching 단가(cache_creation/cache_read)가 DB ORM까지 전달되어야 함."""
        data = PricingRequest(
            input_price_per_1k_tokens=Decimal("0.003"),
            output_price_per_1k_tokens=Decimal("0.015"),
            cache_creation_5m_price_per_1k_tokens=Decimal("0.00375"),
            cache_creation_1h_price_per_1k_tokens=Decimal("0.006"),
            cache_read_price_per_1k_tokens=Decimal("0.0003"),
            effective_from=datetime.now(timezone.utc),
        )
        model = _make_model()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.get_by_alias = AsyncMock(return_value=model)
            repo.close_current_pricing = AsyncMock()
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await model_service.set_pricing(
                mock_session, alias="claude-sonnet", data=data, actor=admin_user
            )

        created = repo.create_pricing.call_args.args[0]
        assert created.cache_creation_5m_price_per_1k_tokens == Decimal("0.00375")
        assert created.cache_creation_1h_price_per_1k_tokens == Decimal("0.006")
        assert created.cache_read_price_per_1k_tokens == Decimal("0.0003")
        assert result.current_pricing is not None
        assert result.current_pricing.cache_creation_5m_price_per_1k_tokens == Decimal("0.00375")
        assert result.current_pricing.cache_creation_1h_price_per_1k_tokens == Decimal("0.006")
        assert result.current_pricing.cache_read_price_per_1k_tokens == Decimal("0.0003")

    async def test_set_pricing_defaults_cache_to_zero(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        """cache_* 필드 생략 시 기본 0 (하위 호환, OPENMODEL 경로 등)."""
        data = PricingRequest(
            input_price_per_1k_tokens=Decimal("0.001"),
            output_price_per_1k_tokens=Decimal("0.002"),
            effective_from=datetime.now(timezone.utc),
        )
        assert data.cache_creation_5m_price_per_1k_tokens == Decimal("0")
        assert data.cache_creation_1h_price_per_1k_tokens == Decimal("0")
        assert data.cache_read_price_per_1k_tokens == Decimal("0")

        model = _make_model()
        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.get_by_alias = AsyncMock(return_value=model)
            repo.close_current_pricing = AsyncMock()
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            await model_service.set_pricing(
                mock_session, alias="llama-3-70b", data=data, actor=admin_user
            )

        created = repo.create_pricing.call_args.args[0]
        assert created.cache_creation_5m_price_per_1k_tokens == Decimal("0")
        assert created.cache_read_price_per_1k_tokens == Decimal("0")


class TestPatchStatus:
    async def test_patch_status_to_inactive_invalidates_cache(
        self, model_service: ModelService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = StatusPatchRequest(active=False)
        model = _make_model()
        model.status = ModelStatus.INACTIVE

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.patch_status = AsyncMock(return_value=model)
            repo.get_current_pricing = AsyncMock(return_value=_make_pricing())
            mock_audit.log = AsyncMock()

            result = await model_service.patch_status(mock_session, alias="claude-sonnet", data=data, actor=admin_user)

        assert result.status == "INACTIVE"
        assert mock_redis.delete.call_count >= 1
