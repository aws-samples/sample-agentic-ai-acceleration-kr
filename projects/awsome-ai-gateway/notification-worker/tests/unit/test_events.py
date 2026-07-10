# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""parse_pubsub_message 단위 테스트."""
from __future__ import annotations

import json

import pytest

from worker.schemas.events import EventType, NotificationEvent, ServiceSource, parse_pubsub_message


def _make_raw(overrides: dict | None = None) -> str:
    base = {
        "event_id": "evt-001",
        "type": "budget_threshold",
        "timestamp": "2026-04-10T12:00:00Z",
        "source": "gateway-proxy",
        "payload": {"user_id": "u1", "threshold_pct": 80},
    }
    if overrides:
        base.update(overrides)
    return json.dumps(base)


def test_parse_valid_message() -> None:
    raw = _make_raw()
    event = parse_pubsub_message(raw)

    assert event is not None
    assert isinstance(event, NotificationEvent)
    assert event.event_id == "evt-001"
    assert event.type == EventType.BUDGET_THRESHOLD
    assert event.source == ServiceSource.GATEWAY_PROXY
    assert event.payload["threshold_pct"] == 80


def test_parse_bytes_input() -> None:
    raw = _make_raw().encode()
    event = parse_pubsub_message(raw)
    assert event is not None
    assert event.type == EventType.BUDGET_THRESHOLD


def test_parse_returns_none_on_invalid_json() -> None:
    result = parse_pubsub_message("not-json")
    assert result is None


def test_parse_returns_none_on_missing_field() -> None:
    raw = json.dumps({"event_id": "e1", "type": "budget_threshold"})  # timestamp/source 누락
    result = parse_pubsub_message(raw)
    assert result is None


def test_parse_returns_none_on_unknown_event_type() -> None:
    raw = _make_raw({"type": "unknown_event_xyz"})
    result = parse_pubsub_message(raw)
    assert result is None


def test_parse_all_event_types() -> None:
    for et in EventType:
        raw = _make_raw({"type": et.value})
        event = parse_pubsub_message(raw)
        assert event is not None, f"Failed to parse event type: {et.value}"
        assert event.type == et


def test_timestamp_z_suffix_parsed_correctly() -> None:
    raw = _make_raw({"timestamp": "2026-04-10T00:00:00Z"})
    event = parse_pubsub_message(raw)
    assert event is not None
    assert event.timestamp.tzinfo is not None
