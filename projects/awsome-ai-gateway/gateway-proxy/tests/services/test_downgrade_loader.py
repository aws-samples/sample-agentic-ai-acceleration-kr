# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.downgrade_loader import (
    CACHE_KEY_FMT,
    CACHE_TTL_SECONDS,
    DowngradePolicyLoader,
    DowngradeRule,
)

TEAM_ID = uuid.uuid4()


@pytest.mark.asyncio
async def test_cache_key_matches_admin_api_convention():
    """admin-api budget_service.py:525가 사용하는 키와 정확히 일치해야 한다."""
    assert CACHE_KEY_FMT.format(team_id=TEAM_ID) == f"budget:downgrade:team:{TEAM_ID}"


@pytest.mark.asyncio
async def test_redis_hit_returns_parsed_rules():
    redis = AsyncMock()
    redis.get.return_value = json.dumps(
        [
            {"from_alias": "opus", "to_alias": "sonnet", "threshold_pct": 80},
        ]
    ).encode()

    loader = DowngradePolicyLoader()
    rules = await loader.get_active_rules(redis, db=None, team_id=TEAM_ID)

    assert rules == [DowngradeRule("opus", "sonnet", 80)]
    redis.get.assert_awaited_once_with(f"budget:downgrade:team:{TEAM_ID}")


@pytest.mark.asyncio
async def test_redis_miss_falls_back_to_db_and_writes_cache():
    redis = AsyncMock()
    redis.get.return_value = None

    db = MagicMock()
    loader = DowngradePolicyLoader()

    async def fake_query(_db, _team_id):
        return [DowngradeRule("opus", "sonnet", 80)]

    loader._query_db = fake_query  # type: ignore[method-assign]
    rules = await loader.get_active_rules(redis, db=db, team_id=TEAM_ID)

    assert rules == [DowngradeRule("opus", "sonnet", 80)]
    redis.setex.assert_awaited_once()
    args, _ = redis.setex.call_args
    assert args[0] == f"budget:downgrade:team:{TEAM_ID}"
    assert args[1] == CACHE_TTL_SECONDS


@pytest.mark.asyncio
async def test_redis_failure_falls_back_to_db():
    redis = AsyncMock()
    redis.get.side_effect = RuntimeError("redis down")

    db = MagicMock()
    loader = DowngradePolicyLoader()

    async def fake_query(_db, _team_id):
        return [DowngradeRule("opus", "sonnet", 80)]

    loader._query_db = fake_query  # type: ignore[method-assign]
    rules = await loader.get_active_rules(redis, db=db, team_id=TEAM_ID)

    assert rules == [DowngradeRule("opus", "sonnet", 80)]


@pytest.mark.asyncio
async def test_both_redis_and_db_fail_raises():
    redis = AsyncMock()
    redis.get.side_effect = RuntimeError("redis down")

    db = MagicMock()
    loader = DowngradePolicyLoader()

    async def fake_query(_db, _team_id):
        raise RuntimeError("db down")

    loader._query_db = fake_query  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await loader.get_active_rules(redis, db=db, team_id=TEAM_ID)


@pytest.mark.asyncio
async def test_no_redis_no_db_returns_empty():
    """모든 의존성이 None이면 안전하게 빈 리스트 반환 (degradation 시나리오)."""
    loader = DowngradePolicyLoader()
    rules = await loader.get_active_rules(redis=None, db=None, team_id=TEAM_ID)
    assert rules == []


@pytest.mark.asyncio
async def test_db_query_orders_by_threshold_pct_ascending():
    """DowngradePolicyLoader가 DB에서 규칙을 가져올 때 threshold_pct ASC 정렬을 보장해야 한다.
    apply_chain이 first-match 의미론을 사용하므로 정렬이 라우팅 결정에 직접 영향한다."""
    import inspect

    from app.services.downgrade_loader import DowngradePolicyLoader as Loader

    # 정적 분석: _query_db 함수의 SQL이 ORDER BY threshold_pct ASC를 포함하는지 inspect
    source = inspect.getsource(Loader._query_db)
    assert "threshold_pct" in source and "asc" in source.lower(), (
        "_query_db must ORDER BY threshold_pct ASC"
    )
