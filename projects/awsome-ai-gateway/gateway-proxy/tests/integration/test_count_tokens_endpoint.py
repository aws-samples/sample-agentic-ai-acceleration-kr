# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""POST /v1/messages/count_tokens — KI-05 regression.

Verifies that a VK-authenticated client can successfully call /v1/messages/count_tokens
and receive {"input_tokens": N} in Anthropic format.

전제: real Postgres + Redis + seeded model aliases + admin-api dev key issuer 가용.
"""

from __future__ import annotations

import os

import pytest
import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
ADMIN_URL = os.environ.get("ADMIN_API_URL", "http://localhost:8080")


@pytest.fixture
def virtual_key():
    resp = httpx.post(
        f"{ADMIN_URL}/internal/test/issue-key",
        json={"email": "test-count-tokens@test.local"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["virtual_key"]


@pytest.mark.integration
def test_count_tokens_auth_required():
    """KI-05: /v1/messages/count_tokens must require auth (401 without token)."""
    resp = httpx.post(
        f"{GATEWAY_URL}/v1/messages/count_tokens",
        json={
            "model": "claude-haiku-4-5",
            "messages": [{"role": "user", "content": "hi"}],
        },
        timeout=10,
    )
    assert resp.status_code == 401


@pytest.mark.integration
def test_count_tokens_accepts_vk_and_returns_input_tokens(virtual_key):
    """KI-05: VK-authenticated request must return 200 + Anthropic-format input_tokens."""
    resp = httpx.post(
        f"{GATEWAY_URL}/v1/messages/count_tokens",
        headers={
            "Authorization": f"Bearer {virtual_key}",
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": "claude-haiku-4-5",
            "messages": [{"role": "user", "content": "hello world"}],
        },
        timeout=15,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "input_tokens" in body
    assert isinstance(body["input_tokens"], int)
    assert body["input_tokens"] > 0


@pytest.mark.integration
def test_count_tokens_unknown_model_returns_404(virtual_key):
    resp = httpx.post(
        f"{GATEWAY_URL}/v1/messages/count_tokens",
        headers={"Authorization": f"Bearer {virtual_key}"},
        json={
            "model": "no-such-alias",
            "messages": [{"role": "user", "content": "hi"}],
        },
        timeout=10,
    )
    assert resp.status_code == 404


@pytest.mark.integration
def test_count_tokens_missing_model_field_returns_400(virtual_key):
    resp = httpx.post(
        f"{GATEWAY_URL}/v1/messages/count_tokens",
        headers={"Authorization": f"Bearer {virtual_key}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
        timeout=10,
    )
    assert resp.status_code == 400
