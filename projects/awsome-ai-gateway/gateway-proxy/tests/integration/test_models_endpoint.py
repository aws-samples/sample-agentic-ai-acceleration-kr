# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GET /v1/models — 응답 스펙 검증.

전제: real Postgres + seed + admin-api dev key issuer 가용.
"""

from __future__ import annotations

import os

import pytest
import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
ADMIN_URL = os.environ.get("ADMIN_API_URL", "http://localhost:8080")


@pytest.fixture
def virtual_key():
    """admin-api dev endpoint으로 VK 발급."""
    resp = httpx.post(
        f"{ADMIN_URL}/internal/test/issue-key",
        json={"email": "test-models-endpoint@test.local"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["virtual_key"]


@pytest.mark.integration
def test_list_models_returns_seeded_aliases(virtual_key):
    resp = httpx.get(
        f"{GATEWAY_URL}/v1/models",
        headers={"Authorization": f"Bearer {virtual_key}"},
        timeout=10,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    aliases = {m["id"] for m in body["data"]}
    assert "claude-sonnet-4-6" in aliases
    assert "claude-opus-4-6" in aliases
    assert "claude-haiku-4-5" in aliases


@pytest.mark.integration
def test_response_includes_metadata_fields(virtual_key):
    resp = httpx.get(
        f"{GATEWAY_URL}/v1/models",
        headers={"Authorization": f"Bearer {virtual_key}"},
        timeout=10,
    )
    assert resp.status_code == 200
    sonnet = next(m for m in resp.json()["data"] if m["id"] == "claude-sonnet-4-6")
    assert sonnet["provider"] == "BEDROCK"
    assert sonnet["api_format"] == "BEDROCK_NATIVE"
    assert sonnet["provider_model_id"] == "global.anthropic.claude-sonnet-4-6"
    assert sonnet["pricing"]["input_per_1k_usd"] == "0.003000"
    assert sonnet["pricing"]["output_per_1k_usd"] == "0.015000"
    assert sonnet["pricing"]["currency"] == "USD"


@pytest.mark.integration
def test_response_keeps_openai_compat_fields(virtual_key):
    resp = httpx.get(
        f"{GATEWAY_URL}/v1/models",
        headers={"Authorization": f"Bearer {virtual_key}"},
        timeout=10,
    )
    sonnet = next(m for m in resp.json()["data"] if m["id"] == "claude-sonnet-4-6")
    assert sonnet["object"] == "model"
    assert sonnet["owned_by"] == "gateway"
    assert isinstance(sonnet["created"], int)
    assert sonnet["created"] > 0


@pytest.mark.integration
def test_auth_required_for_models_endpoint():
    resp = httpx.get(f"{GATEWAY_URL}/v1/models", timeout=10)
    assert resp.status_code == 401
