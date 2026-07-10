# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.routing import RoutingProfileSchema
from app.services.routing_profile_loader import RoutingProfileLoader


@pytest.mark.asyncio
async def test_load_returns_none_when_no_row():
    redis = AsyncMock()
    redis.get.return_value = None
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    loader = RoutingProfileLoader()
    profile = await loader.load(redis, db, "claude-code")
    assert profile is None


@pytest.mark.asyncio
async def test_load_returns_profile_from_cache():
    redis = AsyncMock()
    redis.get.return_value = json.dumps(
        {"client": "cowork", "backend": "mantle", "region": "ap-northeast-1",
         "default_model": "cowork-opus", "account_role_arn": "arn:...:role/x",
         "external_id": "cowork-bedrock", "enabled": True}
    )
    db = MagicMock()
    loader = RoutingProfileLoader()
    profile = await loader.load(redis, db, "cowork")
    assert isinstance(profile, RoutingProfileSchema)
    assert profile.backend == "mantle"
    assert profile.default_model == "cowork-opus"
    db.execute.assert_not_called()  # served from cache


@pytest.mark.asyncio
async def test_load_db_path_maps_row_and_caches():
    # Happy-path DB load on a cold cache: verifies the row->schema field mapping
    # (guards against a transposed field) and the cache write-back.
    redis = AsyncMock()
    redis.get.return_value = None
    row = MagicMock()
    row.client = "cowork"
    row.backend = "mantle"
    row.region = "ap-northeast-1"
    row.default_model = "cowork-opus"
    row.account_role_arn = "arn:aws:iam::234567890123:role/llm-gateway-cowork-bedrock"
    row.external_id = "cowork-bedrock"
    row.enabled = True
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    loader = RoutingProfileLoader()
    profile = await loader.load(redis, db, "cowork")

    assert isinstance(profile, RoutingProfileSchema)
    assert profile.client == "cowork"
    assert profile.backend == "mantle"
    assert profile.region == "ap-northeast-1"
    assert profile.default_model == "cowork-opus"
    assert profile.account_role_arn == "arn:aws:iam::234567890123:role/llm-gateway-cowork-bedrock"
    assert profile.external_id == "cowork-bedrock"
    assert profile.enabled is True
    # cache write-back happened with the routing_profile:<client> key
    redis.setex.assert_called_once()
    assert redis.setex.call_args.args[0] == "routing_profile:cowork"


@pytest.mark.asyncio
async def test_load_disabled_profile_returns_none():
    redis = AsyncMock()
    redis.get.return_value = None
    row = MagicMock()
    row.client = "cowork"; row.backend = "mantle"; row.region = "ap-northeast-1"
    row.default_model = "cowork-opus"; row.account_role_arn = "arn"; row.external_id = "x"
    row.enabled = False
    result = MagicMock(); result.scalar_one_or_none.return_value = row
    db = MagicMock(); db.execute = AsyncMock(return_value=result)

    loader = RoutingProfileLoader()
    profile = await loader.load(redis, db, "cowork")
    assert profile is None  # disabled = treated as no profile (fall back to default path)
