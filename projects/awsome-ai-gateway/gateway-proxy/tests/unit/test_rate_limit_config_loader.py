# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.rate_limit_config_loader import (
    ScopeLimits,
    invalidate_scope_cache,
    load_all_scope_limits,
)


@pytest.mark.asyncio
async def test_cache_hit_skips_db(mock_redis):
    mock_redis.get = AsyncMock(
        side_effect=[
            json.dumps({"rpm": 100, "tpm": 50000}).encode(),   # USER
            json.dumps({"rpm": 500, "tpm": 300000}).encode(),  # TEAM
            json.dumps({"rpm": 5000, "tpm": 1000000}).encode(),  # GLOBAL
        ]
    )
    db_mock = MagicMock()
    db_mock.execute = AsyncMock()

    result = await load_all_scope_limits(
        redis=mock_redis,
        db=db_mock,
        user_id="u1",
        team_id="t1",
        model_alias="claude-opus",
    )

    assert result.user == ScopeLimits(rpm=100, tpm=50000)
    assert result.team == ScopeLimits(rpm=500, tpm=300000)
    assert result.global_ == ScopeLimits(rpm=5000, tpm=1000000)
    # DB 조회 안 됐는지 확인
    db_mock.execute.assert_not_called()


@pytest.mark.asyncio
async def test_cache_miss_falls_back_to_db_and_caches(mock_redis):
    # 모든 스코프 캐시 miss
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()

    # DB 가짜 응답 — 단일 레코드 반환 (모델별 한도)
    from app.models.model import RateLimitConfig

    rows_by_scope = {
        "USER": [
            _mk_config(scope="USER", scope_id="u1", model_alias="claude-opus",
                       rpm=60, tpm=10000),
        ],
        "TEAM": [
            _mk_config(scope="TEAM", scope_id="t1", model_alias=None,
                       rpm=600, tpm=100000),
        ],
        "GLOBAL": [
            _mk_config(scope="GLOBAL", scope_id=None, model_alias="claude-opus",
                       rpm=10000, tpm=1000000),
        ],
    }

    call_count = {"n": 0}

    async def fake_execute(stmt):
        # 3번 호출 — USER, TEAM, GLOBAL 순서
        scopes = ["USER", "TEAM", "GLOBAL"]
        idx = call_count["n"]
        call_count["n"] += 1
        scope = scopes[idx]
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=rows_by_scope[scope])
        result_mock = MagicMock()
        result_mock.scalars = MagicMock(return_value=scalars_mock)
        return result_mock

    db_mock = MagicMock()
    db_mock.execute = fake_execute

    result = await load_all_scope_limits(
        redis=mock_redis,
        db=db_mock,
        user_id="u1",
        team_id="t1",
        model_alias="claude-opus",
    )

    assert result.user == ScopeLimits(rpm=60, tpm=10000)
    assert result.team == ScopeLimits(rpm=600, tpm=100000)
    assert result.global_ == ScopeLimits(rpm=10000, tpm=1000000)

    # 모든 스코프 결과가 캐시에 저장됐는지 확인
    assert mock_redis.set.call_count == 3


@pytest.mark.asyncio
async def test_db_none_returns_unlimited(mock_redis):
    mock_redis.get = AsyncMock(return_value=None)

    result = await load_all_scope_limits(
        redis=mock_redis,
        db=None,  # DB degraded
        user_id="u1",
        team_id="t1",
        model_alias="m1",
    )

    # 모든 스코프 unlimited
    assert result.user.rpm is None and result.user.tpm is None
    assert result.team.rpm is None and result.team.tpm is None
    assert result.global_.rpm is None and result.global_.tpm is None


@pytest.mark.asyncio
async def test_no_team_id_skips_team_scope(mock_redis):
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()

    call_count = {"n": 0}
    scopes_called: list[str] = []

    async def fake_execute(stmt):
        scopes_called.append("called")
        call_count["n"] += 1
        scalars_mock = MagicMock()
        scalars_mock.all = MagicMock(return_value=[])
        result_mock = MagicMock()
        result_mock.scalars = MagicMock(return_value=scalars_mock)
        return result_mock

    db_mock = MagicMock()
    db_mock.execute = fake_execute

    await load_all_scope_limits(
        redis=mock_redis,
        db=db_mock,
        user_id="u1",
        team_id=None,  # 팀 미할당
        model_alias="m1",
    )

    # TEAM 스코프 건너뜀 → 2번 DB 호출 (USER + GLOBAL)
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_invalidate_scope_cache(mock_redis):
    mock_redis.delete = AsyncMock()
    await invalidate_scope_cache(mock_redis, "USER", "u1", "claude-opus")
    mock_redis.delete.assert_called_once_with("rl:config:USER:u1:claude-opus")


def _mk_config(*, scope, scope_id, model_alias, rpm, tpm, cpm=None, cph=None):
    """fake RateLimitConfig row."""
    mock = MagicMock()
    mock.scope = scope
    mock.scope_id = scope_id
    mock.model_alias = model_alias
    mock.rpm_limit = rpm
    mock.tpm_limit = tpm
    mock.cpm_limit_usd = cpm
    mock.cph_limit_usd = cph
    mock.is_active = True
    return mock
