# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.cache_invalidation import CacheInvalidationManager


class TestCacheInvalidation:
    async def test_invalidate_deletes_keys(self):
        redis = AsyncMock()
        redis.delete = AsyncMock()
        mgr = CacheInvalidationManager(redis)

        await mgr.invalidate(["model:claude-sonnet", "model:list"])

        assert redis.delete.call_count == 2
        redis.delete.assert_any_call("model:claude-sonnet")
        redis.delete.assert_any_call("model:list")

    async def test_invalidate_empty_list_noop(self):
        redis = AsyncMock()
        mgr = CacheInvalidationManager(redis)

        await mgr.invalidate([])

        redis.delete.assert_not_called()

    async def test_invalidate_failure_logs_to_db(self):
        redis = AsyncMock()
        redis.delete = AsyncMock(side_effect=Exception("Redis down"))
        session = AsyncMock()
        session.add = MagicMock()

        mgr = CacheInvalidationManager(redis)
        await mgr.invalidate(["model:test"], session=session)

        # Failure recorded to DB
        session.add.assert_called_once()
        recorded = session.add.call_args[0][0]
        assert recorded.cache_key == "model:test"

    async def test_invalidate_failure_without_session_only_logs(self):
        redis = AsyncMock()
        redis.delete = AsyncMock(side_effect=Exception("Redis down"))

        mgr = CacheInvalidationManager(redis)
        # Should not raise even without session
        await mgr.invalidate(["model:test"])


class TestSwapReverseIndexMembership:
    async def test_swap_executes_srem_and_sadd(self):
        fake_redis = MagicMock()
        fake_pipe = MagicMock()
        fake_pipe.execute = AsyncMock(return_value=[1, 2])
        fake_pipe.srem = MagicMock()
        fake_pipe.sadd = MagicMock()
        fake_redis.pipeline = MagicMock(return_value=fake_pipe)

        mgr = CacheInvalidationManager(fake_redis)
        await mgr.swap_reverse_index_membership(
            old_key="team:vk_hashes:OLD",
            new_key="team:vk_hashes:NEW",
            members=["h1", "h2"],
        )

        fake_pipe.srem.assert_called_once_with("team:vk_hashes:OLD", "h1", "h2")
        fake_pipe.sadd.assert_called_once_with("team:vk_hashes:NEW", "h1", "h2")
        fake_pipe.execute.assert_awaited_once()
        fake_redis.pipeline.assert_called_once_with(transaction=False)

    async def test_swap_empty_members_noop(self):
        fake_redis = MagicMock()
        fake_redis.pipeline = MagicMock()  # should not be called

        mgr = CacheInvalidationManager(fake_redis)
        await mgr.swap_reverse_index_membership(
            old_key="team:vk_hashes:OLD",
            new_key="team:vk_hashes:NEW",
            members=[],
        )

        fake_redis.pipeline.assert_not_called()

    async def test_swap_records_failure_and_raises(self, mock_session):
        fake_redis = MagicMock()
        fake_pipe = MagicMock()
        fake_pipe.execute = AsyncMock(side_effect=ConnectionError("boom"))
        fake_pipe.srem = MagicMock()
        fake_pipe.sadd = MagicMock()
        fake_redis.pipeline = MagicMock(return_value=fake_pipe)

        mgr = CacheInvalidationManager(fake_redis)
        mgr._record_failure = AsyncMock()

        with pytest.raises(ConnectionError):
            await mgr.swap_reverse_index_membership(
                old_key="team:vk_hashes:OLD",
                new_key="team:vk_hashes:NEW",
                members=["h1"],
                session=mock_session,
            )

        mgr._record_failure.assert_awaited_once()
        args = mgr._record_failure.call_args.args
        # second positional arg is the cache_key string
        assert args[1] == "swap:team:vk_hashes:OLD->team:vk_hashes:NEW"


class TestRetryFailed:
    async def test_retry_resolves_successful(self):
        redis = AsyncMock()
        redis.delete = AsyncMock()  # Succeeds now

        mgr = CacheInvalidationManager(redis)

        from app.models.audit import CacheInvalidationFailure

        failure = MagicMock(spec=CacheInvalidationFailure)
        failure.cache_key = "model:failed-key"
        failure.resolved_at = None
        failure.retry_count = 0

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [failure]
        session.execute = AsyncMock(return_value=mock_result)

        resolved = await mgr.retry_failed(session)

        assert resolved == 1
        assert failure.resolved_at is not None

    async def test_retry_increments_count_on_failure(self):
        redis = AsyncMock()
        redis.delete = AsyncMock(side_effect=Exception("still down"))

        mgr = CacheInvalidationManager(redis)

        from app.models.audit import CacheInvalidationFailure

        failure = MagicMock(spec=CacheInvalidationFailure)
        failure.cache_key = "model:still-failed"
        failure.resolved_at = None
        failure.retry_count = 2

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [failure]
        session.execute = AsyncMock(return_value=mock_result)

        resolved = await mgr.retry_failed(session)

        assert resolved == 0
        assert failure.retry_count == 3
