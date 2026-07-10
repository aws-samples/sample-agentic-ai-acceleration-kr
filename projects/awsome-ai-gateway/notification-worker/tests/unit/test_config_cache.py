# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ConfigCache 단위 테스트."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker.models.notification import NotificationConfig
from worker.services.config_cache import ConfigCache


def _make_config(event_type: str, enabled: bool = True) -> NotificationConfig:
    cfg = MagicMock(spec=NotificationConfig)
    cfg.event_type = event_type
    cfg.enabled = enabled
    cfg.recipient_roles = ["affected_user"]
    return cfg


async def test_load_populates_cache() -> None:
    """load()는 DB에서 모든 NotificationConfig를 로드한다."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [
        _make_config("budget_threshold"),
        _make_config("key_expiring"),
    ]
    session.execute = AsyncMock(return_value=result_mock)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    cache = ConfigCache(factory)
    await cache.load()

    assert cache.get("budget_threshold") is not None
    assert cache.get("key_expiring") is not None
    assert cache.get("nonexistent") is None


async def test_get_returns_none_before_load() -> None:
    factory = MagicMock()
    cache = ConfigCache(factory)
    assert cache.get("budget_threshold") is None


async def test_reload_updates_existing_cache() -> None:
    session = AsyncMock()

    # 첫 번째 load: budget_threshold만 존재
    result1 = MagicMock()
    result1.scalars.return_value.all.return_value = [_make_config("budget_threshold")]

    # reload (두 번째 load): key_expiring 추가
    result2 = MagicMock()
    result2.scalars.return_value.all.return_value = [
        _make_config("budget_threshold"),
        _make_config("key_expiring"),
    ]

    session.execute = AsyncMock(side_effect=[result1, result2])
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    cache = ConfigCache(factory)
    await cache.load()
    assert cache.get("key_expiring") is None

    await cache.reload()
    assert cache.get("key_expiring") is not None


def test_needs_poll_returns_true_initially() -> None:
    factory = MagicMock()
    cache = ConfigCache(factory)
    # _last_loaded = 0 → 5분 초과 → True
    assert cache.needs_poll() is True


def test_needs_poll_returns_false_after_recent_load() -> None:
    factory = MagicMock()
    cache = ConfigCache(factory)
    cache._last_loaded = time.monotonic()
    assert cache.needs_poll() is False
