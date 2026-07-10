# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Model management integration tests — router + service + mocked DB."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from app.models.model import ApiFormat, ModelAlias, ModelPricing, ModelStatus, Provider


def _make_model(alias: str = "claude-sonnet") -> ModelAlias:
    m = MagicMock(spec=ModelAlias)
    m.alias = alias
    m.provider = Provider.BEDROCK
    m.provider_model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    m.endpoint_url = None
    m.api_format = ApiFormat.BEDROCK_NATIVE
    m.status = ModelStatus.ACTIVE
    m.description = "Test model"
    m.created_at = datetime.now(timezone.utc)
    m.updated_at = datetime.now(timezone.utc)
    return m


def _make_pricing() -> ModelPricing:
    p = MagicMock(spec=ModelPricing)
    p.input_price_per_1k_tokens = Decimal("0.003")
    p.output_price_per_1k_tokens = Decimal("0.015")
    p.cache_creation_5m_price_per_1k_tokens = Decimal("0.00375")
    p.cache_read_price_per_1k_tokens = Decimal("0.0003")
    p.effective_from = datetime.now(timezone.utc)
    p.effective_until = None
    return p


class TestModelCRUD:
    async def test_create_model_returns_201(self, client: AsyncClient, admin_headers: dict):
        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.alias_exists_ci = AsyncMock(return_value=False)
            repo.create_model = AsyncMock()
            repo.create_pricing = AsyncMock()
            mock_audit.log = AsyncMock()

            resp = await client.post(
                "/admin/models",
                json={
                    "alias": "claude-sonnet",
                    "provider": "BEDROCK",
                    "provider_model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "api_format": "BEDROCK_NATIVE",
                    "input_price_per_1k_tokens": "0.003",
                    "output_price_per_1k_tokens": "0.015",
                },
                headers=admin_headers,
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["alias"] == "claude-sonnet"

    async def test_create_duplicate_alias_returns_409(self, client: AsyncClient, admin_headers: dict):
        with patch("app.services.model_service.ModelRepository") as MockRepo:
            MockRepo.return_value.alias_exists_ci = AsyncMock(return_value=True)

            resp = await client.post(
                "/admin/models",
                json={
                    "alias": "existing-model",
                    "provider": "BEDROCK",
                    "provider_model_id": "test",
                    "api_format": "BEDROCK_NATIVE",
                    "input_price_per_1k_tokens": "0.001",
                    "output_price_per_1k_tokens": "0.002",
                },
                headers=admin_headers,
            )

        assert resp.status_code == 409

    async def test_list_models(self, client: AsyncClient, admin_headers: dict):
        model = _make_model()
        pricing = _make_pricing()

        with patch("app.services.model_service.ModelRepository") as MockRepo:
            repo = MockRepo.return_value
            repo.list_all = AsyncMock(return_value=[model])
            repo.get_current_pricing = AsyncMock(return_value=pricing)

            resp = await client.get("/admin/models", headers=admin_headers)

        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1


class TestModelStatusPatch:
    async def test_patch_status_to_inactive(self, client: AsyncClient, admin_headers: dict):
        model = _make_model()
        model.status = ModelStatus.INACTIVE
        pricing = _make_pricing()

        with patch("app.services.model_service.ModelRepository") as MockRepo, \
             patch("app.services.model_service.audit_logger") as mock_audit:
            MockRepo.return_value.patch_status = AsyncMock(return_value=model)
            MockRepo.return_value.get_current_pricing = AsyncMock(return_value=pricing)
            mock_audit.log = AsyncMock()

            resp = await client.patch(
                "/admin/models/claude-sonnet/status",
                json={"active": False},
                headers=admin_headers,
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "INACTIVE"
