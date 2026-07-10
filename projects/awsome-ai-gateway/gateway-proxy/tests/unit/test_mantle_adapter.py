# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.providers.mantle_adapter import MantleAdapter
from app.schemas.routing import RoutingProfileSchema


def _profile():
    return RoutingProfileSchema(
        client="cowork", backend="mantle",
        account_role_arn="arn:aws:iam::234567890123:role/llm-gateway-cowork-bedrock",
        region="ap-northeast-1", default_model="cowork-opus", external_id="cowork-bedrock",
    )


def _adapter(http_client, token="bedrock-api-key-x"):
    broker = MagicMock()
    broker.bearer_token = AsyncMock(return_value=token)
    return MantleAdapter(http_client=http_client, broker=broker)


@pytest.mark.asyncio
async def test_invoke_success_parses_usage_and_sends_bearer():
    resp = httpx.Response(
        200,
        json={"content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
              "usage": {"input_tokens": 16, "output_tokens": 4}},
        request=httpx.Request("POST", "https://x/anthropic/v1/messages"),
    )
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=resp)

    adapter = _adapter(http_client)
    status, body, headers, usage = await adapter.invoke(
        b'{"model":"anthropic.claude-opus-4-8","messages":[]}',
        "anthropic.claude-opus-4-8",
        profile=_profile(),
        endpoint="https://bedrock-mantle.ap-northeast-1.api.aws/anthropic",
    )
    assert status == 200
    assert usage.input_tokens == 16 and usage.output_tokens == 4
    call = http_client.post.call_args
    assert call.args[0].endswith("/v1/messages")
    assert call.kwargs["headers"]["Authorization"] == "Bearer bedrock-api-key-x"
    assert call.kwargs["headers"]["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_invoke_http_error_maps_status():
    resp = httpx.Response(
        403, json={"error": {"message": "denied"}},
        request=httpx.Request("POST", "https://x/anthropic/v1/messages"),
    )
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=resp)
    adapter = _adapter(http_client)
    status, body, headers, usage = await adapter.invoke(
        b"{}", "anthropic.claude-opus-4-8",
        profile=_profile(), endpoint="https://x/anthropic",
    )
    assert status == 403
    assert usage.input_tokens == 0


@pytest.mark.asyncio
async def test_invoke_network_error_returns_502():
    http_client = MagicMock()
    http_client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
    adapter = _adapter(http_client)
    status, body, headers, usage = await adapter.invoke(
        b"{}", "anthropic.claude-opus-4-8",
        profile=_profile(), endpoint="https://x/anthropic",
    )
    assert status == 502


@pytest.mark.asyncio
async def test_stream_yields_raw_event_json_bytes():
    """200 response: only data: JSON payloads are yielded in order."""
    sse_lines = [
        'event: message_start',
        'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}',
        '',
        'event: message_delta',
        'data: {"type":"message_delta","usage":{"output_tokens":5}}',
        '',
    ]

    class _FakeResp:
        status_code = 200

        async def aiter_lines(self):
            for ln in sse_lines:
                yield ln

    class _StreamCM:
        """Async context manager that the new code calls via __aenter__/__aexit__."""
        def __init__(self):
            self._resp = _FakeResp()

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    http_client = MagicMock()
    http_client.stream = MagicMock(return_value=_StreamCM())
    adapter = _adapter(http_client)
    status, chunk_iter, headers, req_id = await adapter.invoke_stream(
        b'{"model":"anthropic.claude-opus-4-8","messages":[],"stream":true}',
        "anthropic.claude-opus-4-8",
        profile=_profile(), endpoint="https://x/anthropic",
    )
    assert status == 200
    chunks = [json.loads(c) async for c in chunk_iter]
    types = [c["type"] for c in chunks]
    assert types == ["message_start", "message_delta"]


@pytest.mark.asyncio
async def test_stream_non_200_propagates_status():
    """Non-200 Mantle response surfaces as the real HTTP status, not buried in 200."""

    class _FakeResp:
        status_code = 429
        aread = AsyncMock()

    class _StreamCM:
        async def __aenter__(self):
            return _FakeResp()

        async def __aexit__(self, *a):
            return False

    http_client = MagicMock()
    http_client.stream = MagicMock(return_value=_StreamCM())
    adapter = _adapter(http_client)
    status, chunk_iter, headers, req_id = await adapter.invoke_stream(
        b"{}", "anthropic.claude-opus-4-8",
        profile=_profile(), endpoint="https://x/anthropic",
    )
    assert status == 429
    chunks = [c async for c in chunk_iter]
    assert len(chunks) == 1
    payload = json.loads(chunks[0])
    assert payload["error"]["type"] == "provider_error"
    assert "429" in payload["error"]["message"]
    # aread must have been called to drain the response body
    _FakeResp.aread.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_auth_failure_returns_502():
    """broker.bearer_token raises → invoke_stream returns 502 with error chunk."""
    http_client = MagicMock()
    broker = MagicMock()
    broker.bearer_token = AsyncMock(side_effect=RuntimeError("token fetch failed"))
    adapter = MantleAdapter(http_client=http_client, broker=broker)
    status, chunk_iter, headers, req_id = await adapter.invoke_stream(
        b"{}", "anthropic.claude-opus-4-8",
        profile=_profile(), endpoint="https://x/anthropic",
    )
    assert status == 502
    chunks = [c async for c in chunk_iter]
    assert len(chunks) == 1
    payload = json.loads(chunks[0])
    assert payload["error"]["type"] == "provider_error"
    assert "auth" in payload["error"]["message"].lower()
