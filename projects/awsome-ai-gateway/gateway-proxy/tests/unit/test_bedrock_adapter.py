# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.providers.bedrock_adapter import BedrockAdapter, _extract_bedrock_usage
from app.schemas.domain import TokenUsage


def make_client_error(code: str):
    return ClientError(
        {"Error": {"Code": code, "Message": "test"}},
        "invoke_model",
    )


def test_extract_bedrock_usage_invoke():
    body = {"usage": {"inputTokens": 100, "outputTokens": 200}}
    usage = _extract_bedrock_usage(body)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 200


def test_extract_bedrock_usage_empty():
    usage = _extract_bedrock_usage({})
    assert usage.input_tokens == 0


@pytest.mark.asyncio
async def test_invoke_validation_error():
    mock_client = MagicMock()
    mock_client.invoke_model.side_effect = make_client_error("ValidationException")

    adapter = BedrockAdapter(mock_client)
    status, body, headers, usage = await adapter.invoke(b"{}", "model-id")
    assert status == 400


@pytest.mark.asyncio
async def test_invoke_throttling():
    mock_client = MagicMock()
    mock_client.invoke_model.side_effect = make_client_error("ThrottlingException")

    adapter = BedrockAdapter(mock_client)
    status, body, headers, usage = await adapter.invoke(b"{}", "model-id")
    assert status == 429


@pytest.mark.asyncio
async def test_count_tokens_success():
    """CountTokens wraps the Anthropic body under input.invokeModel.body and returns inputTokens."""
    mock_client = MagicMock()
    mock_client.count_tokens.return_value = {"inputTokens": 42}

    adapter = BedrockAdapter(mock_client)
    body = b'{"messages":[{"role":"user","content":"hi"}]}'
    status, input_tokens = await adapter.count_tokens(body, "global.anthropic.claude-sonnet-4-6")

    assert status == 200
    assert input_tokens == 42
    mock_client.count_tokens.assert_called_once()
    call_kwargs = mock_client.count_tokens.call_args.kwargs
    assert call_kwargs["modelId"] == "global.anthropic.claude-sonnet-4-6"
    assert call_kwargs["input"] == {"invokeModel": {"body": body}}


@pytest.mark.asyncio
async def test_count_tokens_validation_error():
    mock_client = MagicMock()
    mock_client.count_tokens.side_effect = make_client_error("ValidationException")

    adapter = BedrockAdapter(mock_client)
    status, input_tokens = await adapter.count_tokens(b"{}", "model-id")
    assert status == 400
    assert input_tokens == 0


@pytest.mark.asyncio
async def test_count_tokens_throttling():
    mock_client = MagicMock()
    mock_client.count_tokens.side_effect = make_client_error("ThrottlingException")

    adapter = BedrockAdapter(mock_client)
    status, input_tokens = await adapter.count_tokens(b"{}", "model-id")
    assert status == 429
    assert input_tokens == 0


@pytest.mark.asyncio
async def test_bedrock_stream_gen_yields_chunk_bytes():
    """Sync botocore EventStream → async chunks via run_in_executor."""
    events = [
        {"chunk": {"bytes": b'{"type":"message_start"}'}},
        {"chunk": {"bytes": b'{"type":"content_block_delta"}'}},
        {"chunk": {"bytes": b'{"type":"message_stop"}'}},
    ]

    adapter = BedrockAdapter(MagicMock())
    received = [c async for c in adapter._bedrock_stream_gen(iter(events))]

    assert received == [e["chunk"]["bytes"] for e in events]


@pytest.mark.asyncio
async def test_bedrock_stream_gen_skips_events_without_bytes():
    """Events missing the chunk.bytes field should be silently skipped."""
    events = [
        {"chunk": {"bytes": b'{"type":"message_start"}'}},
        {"metadata": {"model": "x"}},  # no chunk
        {"chunk": {}},  # no bytes
        {"chunk": {"bytes": b'{"type":"message_stop"}'}},
    ]

    adapter = BedrockAdapter(MagicMock())
    received = [c async for c in adapter._bedrock_stream_gen(iter(events))]

    assert received == [
        b'{"type":"message_start"}',
        b'{"type":"message_stop"}',
    ]


@pytest.mark.asyncio
async def test_bedrock_stream_gen_propagates_iteration_error():
    """Sync iterator raising mid-iteration must propagate so the caller
    (bedrock_anthropic_sse_stream) can surface it as an SSE error event."""

    def bad_iter():
        yield {"chunk": {"bytes": b'{"type":"message_start"}'}}
        raise RuntimeError("upstream boom")

    adapter = BedrockAdapter(MagicMock())
    collected: list[bytes] = []
    with pytest.raises(RuntimeError, match="upstream boom"):
        async for c in adapter._bedrock_stream_gen(bad_iter()):
            collected.append(c)

    assert collected == [b'{"type":"message_start"}']
