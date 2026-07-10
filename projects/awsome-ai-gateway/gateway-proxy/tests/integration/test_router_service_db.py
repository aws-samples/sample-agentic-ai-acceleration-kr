# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""real Postgres + seed data 기반 RouterService 검증.

전제: finch compose up -d postgres redis migration 완료 + 03_seed_data.sql 적용됨.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.router_service import RouterService

DB_URL = os.environ.get(
    "TEST_DB_URL",
    "postgresql+asyncpg://gateway:gateway_dev_password@localhost:5432/gateway",
)


@pytest.fixture
async def db_session():
    engine = create_async_engine(DB_URL)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_seeded_alias(db_session):
    rs = RouterService()
    schema = await rs.resolve_bedrock_model(
        redis=None, db=db_session, model_ref="claude-sonnet-4-6"
    )
    assert schema.alias == "claude-sonnet-4-6"
    assert schema.provider_model_id == "global.anthropic.claude-sonnet-4-6"
    assert schema.pricing.input_per_1k == Decimal("0.003000")
    assert schema.pricing.output_per_1k == Decimal("0.015000")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_by_full_provider_model_id(db_session):
    rs = RouterService()
    schema = await rs.resolve_bedrock_model(
        redis=None, db=db_session, model_ref="global.anthropic.claude-sonnet-4-6"
    )
    assert schema.alias == "claude-sonnet-4-6"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unregistered_alias_returns_lookup_error(db_session):
    rs = RouterService()
    with pytest.raises(LookupError, match="not found"):
        await rs.resolve_bedrock_model(redis=None, db=db_session, model_ref="claude-zzz-fictional")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_active_returns_seeded_3(db_session):
    rs = RouterService()
    models = await rs.list_active_models(redis=None, db=db_session)
    aliases = {m.alias for m in models}
    assert "claude-sonnet-4-6" in aliases
    assert "claude-opus-4-6" in aliases
    assert "claude-haiku-4-5" in aliases
