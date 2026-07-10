# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for the Responses-API SSE stream (Codex -> Mantle GPT-5.5).

MantleOpenAIAdapter yields RAW JSON event payloads (data: prefix stripped). This
helper must re-frame each as `event: {type}\\ndata: {json}\\n\\n` (NOT passthrough)
and capture usage from the terminal `response.completed` event (nested under
response.usage), recording reasoning_tokens as a submetric.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from app.schemas.domain import TokenUsage
from app.services.streaming import responses_sse_stream


class FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


async def _aiter(items: list[bytes]) -> AsyncIterator[bytes]:
    for it in items:
        yield it


def _raw(obj: dict) -> bytes:
    # Adapter contract: raw JSON bytes, one event payload per chunk.
    return json.dumps(obj).encode()


@pytest.mark.asyncio
async def test_responses_stream_reframes_and_captures_usage():
    captured: dict = {}

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured["usage"] = u

    chunks = [
        _raw({"type": "response.output_text.delta", "delta": "O"}),
        _raw({"type": "response.output_text.delta", "delta": "K"}),
        _raw({
            "type": "response.completed",
            "response": {"usage": {
                "input_tokens": 14, "output_tokens": 63,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens_details": {"reasoning_tokens": 52},
                "total_tokens": 77,
            }},
        }),
    ]
    out = b""
    async for frame in responses_sse_stream(FakeRequest(), _aiter(chunks), on_usage=on_usage):
        out += frame

    text = out.decode()
    # Each event must be re-framed as proper SSE.
    assert "event: response.output_text.delta\ndata: " in text
    assert "event: response.completed\ndata: " in text
    assert text.endswith("\n\n")
    # Usage captured from the terminal event, with reasoning as a submetric.
    u = captured["usage"]
    assert u.input_tokens == 14 and u.output_tokens == 63 and u.total_tokens == 77
    assert u.reasoning_tokens == 52  # submetric, NOT added to output/total
    assert u.cache_read_input_tokens == 2


@pytest.mark.asyncio
async def test_responses_stream_malformed_json_passthrough_no_crash():
    chunks = [b"not-json-at-all", _raw({"type": "response.completed", "response": {}})]
    out = b""
    async for frame in responses_sse_stream(FakeRequest(), _aiter(chunks)):
        out += frame
    # Malformed chunk is passed through defensively as a data: frame.
    assert b"data: not-json-at-all\n\n" in out
