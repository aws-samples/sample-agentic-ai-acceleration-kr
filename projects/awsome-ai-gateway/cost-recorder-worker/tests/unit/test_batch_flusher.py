# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""BatchFlusher 단위 테스트.

배치 flush 로직의 핵심 경로 검증:
- 빈 entries → no-op
- user+team 스코프별 period 집계 합산
- threshold_triggered 있는 entry → notifications:budget publish
- daily counter pipeline 호출
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from worker.batch_flusher import BatchFlusher
from worker.schemas.cost_stream import CostStreamEntry


def _make_entry(
    request_id: str = "req-1",
    user_id: str = "00000000-0000-0000-0000-000000000001",
    team_id: str = "00000000-0000-0000-0000-000000000002",
    cost: str = "0.01",
    threshold: int | None = None,
) -> CostStreamEntry:
    return CostStreamEntry(
        request_id=request_id,
        user_id=user_id,
        team_id=team_id,
        dept_id="00000000-0000-0000-0000-000000000003",
        model_alias="claude-sonnet-4-6",
        provider="BEDROCK",
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=10,
        cache_read_tokens=5,
        cost_usd=Decimal(cost),
        latency_ms=200,
        is_streaming=False,
        estimated_usage=False,
        requested_at="2026-04-21T10:00:00+00:00",
        completed_at="2026-04-21T10:00:01+00:00",
        period="2026-04",
        date="2026-04-21",
        threshold_triggered=threshold,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_empty_batch_is_noop():
    session_factory = MagicMock()
    redis = MagicMock()
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([])
    session_factory.assert_not_called()
    redis.pipeline.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_flush_inserts_usage_and_upserts_budgets():
    """entries 3개, 같은 user/team/period 이면 budget_usages UPSERT 는 집계 1건씩."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entries = [
        _make_entry(request_id=f"req-{i}", cost="0.10") for i in range(3)
    ]

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    # 3 session.execute 호출: INSERT usage_logs + UPSERT user budget + UPSERT team budget
    assert session.execute.await_count == 3
    assert session.commit.await_count == 1

    # 2번째 호출 = user UPSERT — 합산된 cost (0.30)
    user_call = session.execute.await_args_list[1]
    user_params = user_call.args[1]
    assert len(user_params) == 1  # 단일 (user, period) 그룹
    assert user_params[0]["scope"] == "USER"
    assert user_params[0]["cost"] == "0.30"

    # 3번째 호출 = team UPSERT
    team_call = session.execute.await_args_list[2]
    team_params = team_call.args[1]
    assert team_params[0]["scope"] == "TEAM"
    assert team_params[0]["cost"] == "0.30"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_threshold_triggered_publishes_notification():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entries = [
        _make_entry(request_id="req-normal"),  # no threshold
        _make_entry(request_id="req-80pct", threshold=80),
        _make_entry(request_id="req-100pct", threshold=100),
    ]

    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush(entries)

    # publish는 threshold != None 인 2건만.
    assert redis.publish.await_count == 2
    channels = [call.args[0] for call in redis.publish.await_args_list]
    assert all(c == "notifications:budget" for c in channels)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_daily_counter_pipeline_uses_hash_tag():
    """Redis Cluster hash tag {<user_id>} 가 daily counter 키에 포함."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)

    pipe = MagicMock()
    pipe.execute = AsyncMock()
    pipe.incrbyfloat = MagicMock()
    pipe.incrby = MagicMock()
    pipe.sadd = MagicMock()
    pipe.expire = MagicMock()

    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    redis.publish = AsyncMock()

    entry = _make_entry(user_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    flusher = BatchFlusher(session_factory=session_factory, redis=redis)
    await flusher.flush([entry])

    # incrbyfloat 첫 호출의 key 확인 — hash tag 포함.
    keys_seen = [call.args[0] for call in pipe.incrbyfloat.call_args_list]
    assert any("{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}" in k for k in keys_seen)
