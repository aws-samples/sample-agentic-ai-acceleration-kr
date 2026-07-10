# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)
from app.services.router_service import RouterService


@pytest.fixture
def sample_alias_row():
    row = MagicMock()
    row.alias = "claude-sonnet-4-6"
    row.provider = "BEDROCK"
    row.provider_model_id = "global.anthropic.claude-sonnet-4-6"
    row.endpoint_url = None
    row.api_format = "BEDROCK_NATIVE"
    row.status = "ACTIVE"
    row.description = "Claude Sonnet 4.6"
    row.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return row


@pytest.fixture
def sample_pricing_row():
    row = MagicMock()
    row.input_price_per_1k_tokens = Decimal("0.003000")
    row.output_price_per_1k_tokens = Decimal("0.015000")
    # FR-3.3 Stage 1 cache 단가 — Bedrock Prompt Caching: ×1.25 / ×0.1 multiplier.
    row.cache_creation_5m_price_per_1k_tokens = Decimal("0.003750")
    # Must be a real Decimal: _orm_to_schema reads this into ModelPricingSchema
    # (cache_write_1h_per_1k). Leaving it as an auto-MagicMock attribute caused a
    # pydantic Decimal ValidationError in the cache-miss/DB-rebuild path.
    row.cache_creation_1h_price_per_1k_tokens = Decimal("0.006000")
    row.cache_read_price_per_1k_tokens = Decimal("0.000300")
    return row


def _make_db_mock(scalar_results: list):
    """sqlalchemy execute → result.scalar_one_or_none() 모킹.

    scalar_results: 호출 순서대로 반환할 row 리스트 (None 가능).
    """
    db = AsyncMock()
    results = []
    for row in scalar_results:
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=row)
        r.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[row] if row else []))
        )
        results.append(r)
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alias_lookup_success(sample_alias_row, sample_pricing_row):
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    assert schema.alias == "claude-sonnet-4-6"
    assert schema.provider_model_id == "global.anthropic.claude-sonnet-4-6"
    assert schema.provider == ProviderType.BEDROCK
    assert schema.api_format == ApiFormat.BEDROCK_NATIVE
    assert schema.pricing.input_per_1k == Decimal("0.003000")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_model_id_lookup_success(sample_alias_row, sample_pricing_row):
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis, db, "global.anthropic.claude-sonnet-4-6")

    assert schema.alias == "claude-sonnet-4-6"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_hit_skips_db():
    cached_schema = ModelConfigSchema(
        provider_model_id="global.anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    )
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=cached_schema.model_dump_json())
    redis.setex = AsyncMock()
    db = AsyncMock()
    db.execute = AsyncMock()

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    assert schema.alias == "claude-sonnet-4-6"
    db.execute.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_miss_writes_both_keys(sample_alias_row, sample_pricing_row):
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    keys_set = [call.args[0] for call in redis.setex.call_args_list]
    assert "model:claude-sonnet-4-6" in keys_set
    assert "model:global.anthropic.claude-sonnet-4-6" in keys_set


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poison_flat_cache_falls_through_to_db(sample_alias_row, sample_pricing_row):
    """P0-④: a malformed/legacy FLAT cache entry (no nested `pricing`) must NOT
    raise (which surfaced as a permanent 500); it is treated as a cache miss and
    rebuilt from the DB with the correct nested shape + TTL (self-heal)."""
    # The exact flat shape the old admin create_model wrote — missing `pricing`.
    poison = json.dumps({
        "alias": "claude-sonnet-4-6",
        "provider": "BEDROCK",
        "provider_model_id": "global.anthropic.claude-sonnet-4-6",
        "endpoint_url": None,
        "api_format": "BEDROCK_NATIVE",
        "status": "ACTIVE",
        "input_price_per_1k": "0.003",
        "output_price_per_1k": "0.015",
    })
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=poison)
    redis.setex = AsyncMock()
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")

    # Did NOT raise; rebuilt from DB with valid nested pricing.
    assert schema.alias == "claude-sonnet-4-6"
    assert schema.pricing.input_per_1k == Decimal("0.003000")
    # DB was consulted (fell through) and cache was rewritten with correct shape+TTL.
    db.execute.assert_awaited()
    keys_set = [call.args[0] for call in redis.setex.call_args_list]
    assert "model:claude-sonnet-4-6" in keys_set


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unregistered_alias_raises_lookup_error():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    # Two DB queries on a miss: alias exact-match, then provider_model_id fallback.
    db = _make_db_mock([None, None])

    rs = RouterService()
    with pytest.raises(LookupError, match="not found"):
        await rs.resolve_bedrock_model(redis, db, "claude-fictional")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unregistered_full_id_raises_lookup_error():
    """회귀 검증: 기존엔 '.'이 있으면 통과시켰음. 이제는 무조건 LookupError."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    # Two DB queries on a miss: alias exact-match, then provider_model_id fallback.
    db = _make_db_mock([None, None])

    rs = RouterService()
    with pytest.raises(LookupError, match="not found"):
        await rs.resolve_bedrock_model(redis, db, "global.anthropic.claude-fictional")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inactive_alias_raises(sample_alias_row):
    sample_alias_row.status = "INACTIVE"
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    db = _make_db_mock([sample_alias_row])

    rs = RouterService()
    with pytest.raises(LookupError, match="inactive"):
        await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_none_falls_to_db(sample_alias_row, sample_pricing_row):
    """NFR-2.4 graceful degradation: Redis 다운 시 DB 직접 조회."""
    db = _make_db_mock([sample_alias_row, sample_pricing_row])

    rs = RouterService()
    schema = await rs.resolve_bedrock_model(redis=None, db=db, model_ref="claude-sonnet-4-6")
    assert schema.alias == "claude-sonnet-4-6"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_db_exception_propagates():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("DB connection lost"))

    rs = RouterService()
    with pytest.raises(RuntimeError, match="DB connection lost"):
        await rs.resolve_bedrock_model(redis, db, "claude-sonnet-4-6")


# ── FR-2.6 check_key_scope ──


def _schema(alias: str, provider_model_id: str) -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id=provider_model_id,
        alias=alias,
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="",
        pricing=ModelPricingSchema(input_per_1k=Decimal("0"), output_per_1k=Decimal("0")),
        status=ModelStatus.ACTIVE,
    )


def _auth(allowed: list[str] | None):
    from app.schemas.domain import AuthContext, AuthType, Role

    return AuthContext(
        user_id="u1",
        team_id="t1",
        dept_id="",
        roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
        allowed_models=allowed,
    )


def test_check_key_scope_none_allows_everything():
    RouterService().check_key_scope(_auth(None), _schema("claude-opus", "bedrock-id"))


def test_check_key_scope_schema_matches_alias():
    """allowed_models에 alias 있으면 provider_model_id가 달라도 통과."""
    RouterService().check_key_scope(
        _auth(["claude-haiku"]),
        _schema("claude-haiku", "anthropic.claude-3-haiku-20240307-v1:0"),
    )


def test_check_key_scope_schema_matches_provider_model_id():
    """allowed_models에 provider_model_id만 있어도 통과 (하위 호환)."""
    RouterService().check_key_scope(
        _auth(["anthropic.claude-3-haiku-20240307-v1:0"]),
        _schema("claude-haiku", "anthropic.claude-3-haiku-20240307-v1:0"),
    )


def test_check_key_scope_schema_rejects_non_allowed():
    with pytest.raises(PermissionError):
        RouterService().check_key_scope(
            _auth(["claude-haiku"]),
            _schema("claude-opus", "anthropic.claude-3-opus"),
        )


def test_check_key_scope_legacy_str_signature_still_works():
    RouterService().check_key_scope(_auth(["claude-haiku"]), "claude-haiku")
    with pytest.raises(PermissionError):
        RouterService().check_key_scope(_auth(["claude-haiku"]), "claude-opus")
