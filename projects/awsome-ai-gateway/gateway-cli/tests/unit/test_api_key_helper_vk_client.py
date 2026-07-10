# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for api_key_helper.vk_client — Virtual Key request."""

from __future__ import annotations

import pytest
import responses

from api_key_helper.config import HelperConfig
from api_key_helper.vk_client import request_virtual_key


@responses.activate
def test_request_virtual_key_success() -> None:
    responses.add(
        responses.POST,
        "https://gw.example.com/cli/auth/virtual-key",
        json={
            "virtual_key": "vk-test-12345",
            "expires_at": "2026-04-10T18:00:00Z",
            "gateway_endpoint": "https://gw.example.com",
            "otel_endpoint": "https://otel.example.com",
            "user_id": "user-001",
            "team_id": "team-001",
            "max_budget_usd": "100.00",
            "used_usd": "12.50",
            "tpm_limit": 100000,
            "rpm_limit": 60,
        },
        status=200,
    )

    config = HelperConfig(gateway_url="https://gw.example.com")
    sts_request = {"url": "https://sts.ap-northeast-2.amazonaws.com/", "headers": {}}
    result = request_virtual_key(config, sts_request, "my-laptop")

    assert result.virtual_key == "vk-test-12345"
    assert result.user_id == "user-001"
    assert result.tpm_limit == 100000


@responses.activate
def test_request_virtual_key_api_error() -> None:
    responses.add(
        responses.POST,
        "https://gw.example.com/cli/auth/virtual-key",
        json={"error": "unauthorized"},
        status=401,
    )

    config = HelperConfig(gateway_url="https://gw.example.com")
    sts_request = {"url": "https://sts.example.com/", "headers": {}}

    with pytest.raises(Exception):
        request_virtual_key(config, sts_request, "device")


@responses.activate
def test_request_virtual_key_network_error() -> None:
    responses.add(
        responses.POST,
        "https://gw.example.com/cli/auth/virtual-key",
        body=ConnectionError("refused"),
    )

    config = HelperConfig(gateway_url="https://gw.example.com")
    sts_request = {"url": "https://sts.example.com/", "headers": {}}

    with pytest.raises(Exception):
        request_virtual_key(config, sts_request, "device")
