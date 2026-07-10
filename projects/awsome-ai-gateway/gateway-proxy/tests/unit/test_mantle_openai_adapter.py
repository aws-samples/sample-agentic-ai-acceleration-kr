# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.providers.mantle_openai_adapter import MantleOpenAIAdapter, _extract_responses_usage
from app.schemas.routing import RoutingProfileSchema


def _profile():
    # Codex = in-account (account_role_arn NULL), Ohio, GPT-5.5.
    return RoutingProfileSchema(
        client="codex", backend="mantle",
        account_role_arn=None,
        region="us-east-2", default_model="codex-gpt", external_id=None,
    )


def _adapter(http_client, token="bedrock-api-key-codex"):
    broker = MagicMock()
    broker.bearer_token = AsyncMock(return_value=token)
    return MantleOpenAIAdapter(http_client=http_client, broker=broker)


def test_extract_responses_usage_reasoning_and_cache():
    body = {
        "usage": {
            "input_tokens": 11,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens": 17,
            "output_tokens_details": {"reasoning_tokens": 10},
            "total_tokens": 28,
        }
    }
    u = _extract_responses_usage(body)
    assert u.input_tokens == 11
    assert u.output_tokens == 17  # reasoning is INSIDE output, not added on top
    assert u.total_tokens == 28
    assert u.cache_read_input_tokens == 3
    assert u.reasoning_tokens == 10


def test_extract_responses_usage_missing_total_is_derived():
    u = _extract_responses_usage({"usage": {"input_tokens": 5, "output_tokens": 7}})
    assert u.total_tokens == 12  # derived when total absent
    assert u.reasoning_tokens == 0


@pytest.mark.asyncio
async def test_invoke_hits_responses_endpoint_no_anthropic_header():
    resp = httpx.Response(
        200,
        json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
              "status": "completed",
              "usage": {"input_tokens": 8, "output_tokens": 2,
                        "output_tokens_details": {"reasoning_tokens": 1}, "total_tokens": 10}},
        request=httpx.Request("POST", "https://x/openai/v1/responses"),
    )
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=resp)

    adapter = _adapter(http_client)
    status, body, headers, usage = await adapter.invoke(
        b'{"model":"openai.gpt-5.5","input":"hi"}',
        "openai.gpt-5.5",
        profile=_profile(),
        endpoint="https://bedrock-mantle.us-east-2.api.aws/openai",
    )
    assert status == 200
    assert usage.input_tokens == 8 and usage.output_tokens == 2 and usage.reasoning_tokens == 1
    call = http_client.post.call_args
    assert call.args[0].endswith("/v1/responses")
    assert call.kwargs["headers"]["Authorization"] == "Bearer bedrock-api-key-codex"
    # OpenAI Responses must NOT carry the anthropic-version header.
    assert "anthropic-version" not in call.kwargs["headers"]


@pytest.mark.asyncio
async def test_invoke_http_error_maps_status():
    resp = httpx.Response(
        403, json={"error": {"message": "denied"}},
        request=httpx.Request("POST", "https://x/openai/v1/responses"),
    )
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=resp)
    adapter = _adapter(http_client)
    status, body, headers, usage = await adapter.invoke(
        b"{}", "openai.gpt-5.5", profile=_profile(), endpoint="https://x/openai",
    )
    assert status == 403
    assert usage.input_tokens == 0


@pytest.mark.asyncio
async def test_invoke_network_error_returns_502():
    http_client = MagicMock()
    http_client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
    adapter = _adapter(http_client)
    status, body, headers, usage = await adapter.invoke(
        b"{}", "openai.gpt-5.5", profile=_profile(), endpoint="https://x/openai",
    )
    assert status == 502


@pytest.mark.asyncio
async def test_stream_yields_raw_event_json_bytes():
    sse_lines = [
        'event: response.output_text.delta',
        'data: {"type":"response.output_text.delta","delta":"OK"}',
        '',
        'event: response.completed',
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":8,"output_tokens":2}}}',
        '',
        'data: [DONE]',
    ]

    class _FakeResp:
        status_code = 200

        async def aiter_lines(self):
            for ln in sse_lines:
                yield ln

    class _StreamCM:
        async def __aenter__(self):
            return _FakeResp()

        async def __aexit__(self, *a):
            return False

    http_client = MagicMock()
    http_client.stream = MagicMock(return_value=_StreamCM())
    adapter = _adapter(http_client)
    status, chunk_iter, headers, req_id = await adapter.invoke_stream(
        b'{"model":"openai.gpt-5.5","input":"hi","stream":true}',
        "openai.gpt-5.5", profile=_profile(), endpoint="https://x/openai",
    )
    assert status == 200
    chunks = [json.loads(c) async for c in chunk_iter]
    types = [c["type"] for c in chunks]
    # [DONE] is skipped; the two typed events pass through in order.
    assert types == ["response.output_text.delta", "response.completed"]


@pytest.mark.asyncio
async def test_stream_non_200_propagates_status():
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
        b"{}", "openai.gpt-5.5", profile=_profile(), endpoint="https://x/openai",
    )
    assert status == 429
    chunks = [c async for c in chunk_iter]
    payload = json.loads(chunks[0])
    assert payload["error"]["type"] == "provider_error"
    assert "429" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_stream_auth_failure_returns_502():
    http_client = MagicMock()
    broker = MagicMock()
    broker.bearer_token = AsyncMock(side_effect=RuntimeError("token fetch failed"))
    adapter = MantleOpenAIAdapter(http_client=http_client, broker=broker)
    status, chunk_iter, headers, req_id = await adapter.invoke_stream(
        b"{}", "openai.gpt-5.5", profile=_profile(), endpoint="https://x/openai",
    )
    assert status == 502
    chunks = [c async for c in chunk_iter]
    payload = json.loads(chunks[0])
    assert "auth" in payload["error"]["message"].lower()
