# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _reset_rl_breaker():
    """rate-limit 회로 차단기 싱글톤을 테스트마다 초기화(상태 누수 방지, deepdive Q50)."""
    from app.services import rate_limit_service as rls

    rls.reset_breaker_for_test()
    yield
    rls.reset_breaker_for_test()


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    redis.eval = AsyncMock(
        return_value=b'{"allowed":true,"remaining":59,"limit":60,"retry_after":null,"window_reset":0}'
    )
    redis.publish = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    redis.incrbyfloat = AsyncMock()
    redis.incrby = AsyncMock()
    redis.expire = AsyncMock()
    pipe = MagicMock()
    pipe.incrbyfloat = MagicMock()
    pipe.incrby = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    redis.pipeline = MagicMock(return_value=pipe)
    return redis


@pytest.fixture
def mock_db_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.add_all = MagicMock()
    return session


@pytest.fixture
def mock_session_factory(mock_db_session):
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=mock_db_session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.fixture
def auth_context_vk():
    from app.schemas.domain import AuthContext, AuthType, Role

    return AuthContext(
        user_id="user-123",
        team_id="team-456",
        dept_id="dept-789",
        roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
        key_id="key-001",
        allowed_models=None,
    )


@pytest.fixture
def model_config_bedrock():
    from decimal import Decimal
    from app.schemas.domain import (
        ApiFormat,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
    )

    return ModelConfigSchema(
        provider_model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="us-east-1",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015"), cache_write_per_1k=Decimal("0.00375"), cache_write_1h_per_1k=Decimal("0.006"), cache_read_per_1k=Decimal("0.0003")),
        status=ModelStatus.ACTIVE,
    )


@pytest.fixture
def model_config_openai():
    from decimal import Decimal
    from app.schemas.domain import (
        ApiFormat,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
    )

    return ModelConfigSchema(
        provider_model_id="meta-llama/Llama-3.1-70B-Instruct",
        alias="llama-70b",
        provider=ProviderType.OPENMODEL,
        api_format=ApiFormat.OPENAI_COMPATIBLE,
        endpoint="http://mock-vllm:8080",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.001"), output_per_1k=Decimal("0.002")),
        status=ModelStatus.ACTIVE,
    )
