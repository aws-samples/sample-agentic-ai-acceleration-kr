# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for statusline.usage_client — Gateway usage API."""

from __future__ import annotations

from decimal import Decimal

import pytest
import responses

from statusline.config import StatuslineConfig
from statusline.usage_client import fetch_usage


@responses.activate
def test_fetch_usage_success() -> None:
    responses.add(
        responses.GET,
        "https://gw.example.com/v1/usage/me",
        json={
            "used": "12.50",
            "limit": "100.00",
            "remaining": "87.50",
            "percentage": 12.5,
            "period": "2026-04",
        },
        status=200,
    )

    config = StatuslineConfig(gateway_url="https://gw.example.com")
    result = fetch_usage(config, "vk-test")

    assert result.used == Decimal("12.50")
    assert result.limit == Decimal("100.00")
    assert result.remaining == Decimal("87.50")
    assert result.percentage == 12.5
    assert result.period == "2026-04"
    assert result.fetched_at is not None


@responses.activate
def test_fetch_usage_unauthorized() -> None:
    responses.add(
        responses.GET,
        "https://gw.example.com/v1/usage/me",
        json={"error": "unauthorized"},
        status=401,
    )

    config = StatuslineConfig(gateway_url="https://gw.example.com")
    with pytest.raises(Exception):
        fetch_usage(config, "invalid-key")


@responses.activate
def test_fetch_usage_network_error() -> None:
    responses.add(
        responses.GET,
        "https://gw.example.com/v1/usage/me",
        body=ConnectionError("refused"),
    )

    config = StatuslineConfig(gateway_url="https://gw.example.com")
    with pytest.raises(Exception):
        fetch_usage(config, "vk-test")
