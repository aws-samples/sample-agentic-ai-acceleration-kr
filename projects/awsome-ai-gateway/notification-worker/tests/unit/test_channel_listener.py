# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ChannelListener 단위 테스트 — Redis와 handler를 Mock으로 대체."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker.listeners.channel_listener import ChannelListener
from worker.schemas.events import EventType, NotificationEvent, ServiceSource


def _make_raw_message(event_id: str = "e1") -> dict:
    return {
        "type": "message",
        "data": json.dumps({
            "event_id": event_id,
            "type": "budget_threshold",
            "timestamp": "2026-04-10T12:00:00Z",
            "source": "gateway-proxy",
            "payload": {"user_id": "u1", "threshold_pct": 80},
        }),
    }


async def _run_listener_with_messages(messages: list[dict], handler) -> None:
    """지정된 메시지들을 처리한 후 CancelledError로 종료되는 리스너를 실행한다."""
    pubsub = AsyncMock()

    async def listen_gen():
        for msg in messages:
            yield msg
        # CancelledError로 루프 종료 시뮬레이션
        raise asyncio.CancelledError()

    pubsub.listen = listen_gen
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()

    mock_redis = MagicMock()
    mock_redis.pubsub.return_value = pubsub

    with patch("worker.listeners.channel_listener.get_redis_client", return_value=mock_redis):
        with pytest.raises(asyncio.CancelledError):
            listener = ChannelListener("notifications:budget", handler)
            await listener.run()

    return pubsub


async def test_listener_calls_handler_on_valid_message() -> None:
    handler = AsyncMock()
    handler.handle = AsyncMock()

    await _run_listener_with_messages([_make_raw_message("e1")], handler)

    handler.handle.assert_awaited_once()
    event_arg = handler.handle.await_args[0][0]
    assert isinstance(event_arg, NotificationEvent)
    assert event_arg.event_id == "e1"


async def test_listener_skips_non_message_type() -> None:
    handler = AsyncMock()
    handler.handle = AsyncMock()

    # type != "message"인 메시지 (subscribe 확인 메시지 등)
    await _run_listener_with_messages(
        [{"type": "subscribe", "data": 1}],
        handler,
    )

    handler.handle.assert_not_awaited()


async def test_listener_skips_invalid_json() -> None:
    handler = AsyncMock()
    handler.handle = AsyncMock()

    bad_msg = {"type": "message", "data": "not-valid-json"}
    await _run_listener_with_messages([bad_msg], handler)

    handler.handle.assert_not_awaited()


async def test_listener_increments_received_metric() -> None:
    class FakeCounter:
        calls: list = []
        def add(self, v, attrs=None): self.calls.append((v, attrs))

    class FakeMetrics:
        events_received_total = FakeCounter()
        errors_total = FakeCounter()

    handler = AsyncMock()
    handler.handle = AsyncMock()

    pubsub = AsyncMock()

    async def listen_gen():
        yield _make_raw_message("e1")
        raise asyncio.CancelledError()

    pubsub.listen = listen_gen
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()

    mock_redis = MagicMock()
    mock_redis.pubsub.return_value = pubsub
    metrics = FakeMetrics()

    with patch("worker.listeners.channel_listener.get_redis_client", return_value=mock_redis):
        with pytest.raises(asyncio.CancelledError):
            listener = ChannelListener("notifications:budget", handler, metrics=metrics)
            await listener.run()

    assert len(metrics.events_received_total.calls) == 1
