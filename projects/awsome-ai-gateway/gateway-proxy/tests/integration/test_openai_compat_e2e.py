# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""FR-1.4 A안 — /v1/chat/completions E2E against mock-vllm.

Covers:
- Auth required (401 without VK)
- stream=false: OpenAI-format JSON response with usage block
- stream=true: SSE frames + final usage chunk + [DONE]
- Unknown alias → 404
- Invalid JSON body → 400

전제: full stack running (finch compose up -d) — gateway-proxy:8000,
admin-api:8080, mock-vllm, seeded OPENMODEL alias `llama-3-70b`.

NOTE: Week 3 JWT가 붙으면 `/v1/chat/completions` auth 매핑이 JWT 또는
JWT+VK DUAL로 바뀌므로 이 테스트의 VK fixture도 JWT fixture로 전환 필요.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
ADMIN_URL = os.environ.get("ADMIN_API_URL", "http://localhost:8080")

OPENMODEL_ALIAS = "llama-3-70b"


@pytest.fixture
def virtual_key():
    resp = httpx.post(
        f"{ADMIN_URL}/internal/test/issue-key",
        json={"email": "test-openai-compat@test.local"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["virtual_key"]


@pytest.mark.integration
def test_chat_completions_auth_required():
    resp = httpx.post(
        f"{GATEWAY_URL}/v1/chat/completions",
        json={
            "model": OPENMODEL_ALIAS,
            "messages": [{"role": "user", "content": "hi"}],
        },
        timeout=10,
    )
    assert resp.status_code == 401


@pytest.mark.integration
def test_chat_completions_non_stream_returns_openai_body(virtual_key):
    resp = httpx.post(
        f"{GATEWAY_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {virtual_key}"},
        json={
            "model": OPENMODEL_ALIAS,
            "messages": [{"role": "user", "content": "Say hi"}],
        },
        timeout=15,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] >= 1
    # model field: pass-through of provider_model_id (mock-vllm echoes it)
    assert "Llama" in body["model"]


@pytest.mark.integration
def test_chat_completions_stream_emits_sse_frames_and_usage(virtual_key):
    with httpx.stream(
        "POST",
        f"{GATEWAY_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {virtual_key}"},
        json={
            "model": OPENMODEL_ALIAS,
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        },
        timeout=15,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join(resp.iter_bytes())

    text = body.decode()
    frames = [line for line in text.split("\n\n") if line.strip()]
    assert any(f.startswith("data: ") for f in frames)
    assert "[DONE]" in text

    # Pull the last SSE data frame with JSON payload → must carry usage
    usage_frame = None
    for f in reversed(frames):
        if not f.startswith("data: ") or "[DONE]" in f:
            continue
        payload = f.removeprefix("data: ").strip()
        data = json.loads(payload)
        if "usage" in data and data["usage"]:
            usage_frame = data
            break
    assert usage_frame is not None, "expected a final SSE chunk with usage"
    assert usage_frame["usage"]["total_tokens"] >= 1


@pytest.mark.integration
def test_chat_completions_unknown_alias_returns_404(virtual_key):
    resp = httpx.post(
        f"{GATEWAY_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {virtual_key}"},
        json={
            "model": "no-such-openmodel",
            "messages": [{"role": "user", "content": "hi"}],
        },
        timeout=10,
    )
    assert resp.status_code == 404


@pytest.mark.integration
def test_chat_completions_invalid_json_returns_400(virtual_key):
    resp = httpx.post(
        f"{GATEWAY_URL}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {virtual_key}",
            "Content-Type": "application/json",
        },
        content=b"not json",
        timeout=10,
    )
    assert resp.status_code == 400
