# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for POST /v1/messages/count_tokens router.

Mocks the provider registry, RouterService, and request state to exercise the
router logic in isolation (bypassing auth/rate-limit/budget middleware).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.providers.registry import ProviderRegistry
from app.routers import messages as messages_router
from decimal import Decimal

from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)


MODEL_ALIAS = "claude-sonnet-4-6"
PROVIDER_MODEL_ID = "global.anthropic.claude-sonnet-4-6"


def _model_config() -> ModelConfigSchema:
    return ModelConfigSchema(
        alias=MODEL_ALIAS,
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        provider_model_id=PROVIDER_MODEL_ID,
        endpoint="us-east-1",
        status=ModelStatus.ACTIVE,
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.003"),
            output_per_1k=Decimal("0.015"),
        ),
    )


def _build_app(adapter, resolve_return=None, resolve_exc=None):
    """Build a FastAPI app with the count_tokens route wired up.

    No middleware — we inject state via a lightweight dependency.
    """
    app = FastAPI()
    registry = ProviderRegistry()
    registry.register(ProviderType.BEDROCK, adapter)
    app.state.provider_registry = registry

    # Patch RouterService used inside the router
    svc = messages_router._router_service
    if resolve_exc is not None:
        svc.resolve_bedrock_model = AsyncMock(side_effect=resolve_exc)
    else:
        svc.resolve_bedrock_model = AsyncMock(return_value=resolve_return or _model_config())
    svc.check_key_scope = MagicMock()

    @app.middleware("http")
    async def inject_state(request, call_next):
        request.scope["state"] = {
            "auth_context": None,
            "_redis": None,
            "_session_factory": None,
            "_degradation_manager": None,
            "request_id": "test-req",
        }
        return await call_next(request)

    app.include_router(messages_router.router)
    return app


@pytest.mark.asyncio
async def test_count_tokens_success():
    adapter = MagicMock()
    adapter.count_tokens = AsyncMock(return_value=(200, 42))

    app = _build_app(adapter)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": MODEL_ALIAS,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"input_tokens": 42}

    # Adapter was called with the region-prefix-stripped model id
    # (global.anthropic.claude-sonnet-4-6 → anthropic.claude-sonnet-4-6)
    adapter.count_tokens.assert_called_once()
    body_arg, model_id_arg = adapter.count_tokens.call_args.args
    assert model_id_arg == "anthropic.claude-sonnet-4-6"
    # anthropic_version gets injected; extraneous fields like "model" stripped
    import json as _json

    parsed = _json.loads(body_arg)
    assert parsed["anthropic_version"] == "bedrock-2023-05-31"
    assert "model" not in parsed
    assert parsed["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_count_tokens_missing_model():
    adapter = MagicMock()
    adapter.count_tokens = AsyncMock()

    app = _build_app(adapter)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code == 400
    adapter.count_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_count_tokens_invalid_json():
    adapter = MagicMock()
    adapter.count_tokens = AsyncMock()

    app = _build_app(adapter)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 400
    adapter.count_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_count_tokens_unregistered_model():
    adapter = MagicMock()
    adapter.count_tokens = AsyncMock()

    app = _build_app(adapter, resolve_exc=LookupError("unknown alias"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code == 404
    adapter.count_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_count_tokens_bedrock_error():
    adapter = MagicMock()
    adapter.count_tokens = AsyncMock(return_value=(429, 0))

    app = _build_app(adapter)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json={"model": MODEL_ALIAS, "messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code == 429
