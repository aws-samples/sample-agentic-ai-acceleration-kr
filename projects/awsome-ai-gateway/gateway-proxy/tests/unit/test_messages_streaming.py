# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for Bedrock → Anthropic SSE streaming (FR-1.3 edge cases).

Covers:
- happy path: Bedrock EventStream chunks → Anthropic SSE formatted output,
  usage aggregated from message_start + message_delta
- client disconnect mid-stream: generator stops early, background drain
  still runs through the remaining chunks and records usage
- upstream error mid-stream: emits `event: error` SSE event then terminates
- idle timeout: chunk arrives slower than idle limit → `event: error` SSE
  with timeout message
- malformed JSON chunk: raw passthrough (not fatal)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from app.schemas.domain import TokenUsage
from app.services.streaming import bedrock_anthropic_sse_stream


class FakeRequest:
    """Minimal fake Starlette Request supporting is_disconnected()."""

    def __init__(self, disconnect_after: int | None = None) -> None:
        self._disconnect_after = disconnect_after
        self._calls = 0

    async def is_disconnected(self) -> bool:
        self._calls += 1
        if self._disconnect_after is None:
            return False
        return self._calls > self._disconnect_after


async def _aiter(items: list[bytes]) -> AsyncIterator[bytes]:
    for it in items:
        yield it


def _sse_chunks(events: list[dict]) -> list[bytes]:
    """Convert Anthropic event dicts to raw Bedrock chunk bytes
    (the adapter emits one JSON blob per event, not pre-formatted SSE).
    """
    return [json.dumps(e).encode() for e in events]


@pytest.mark.asyncio
async def test_streaming_happy_path_formats_sse_and_aggregates_usage():
    chunks = _sse_chunks(
        [
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 2,
                        "cache_read_input_tokens": 3,
                    }
                },
            },
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
            {
                "type": "message_delta",
                "usage": {"output_tokens": 7},
            },
        ]
    )
    captured: list[TokenUsage] = []

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)

    out: list[bytes] = []
    async for b in bedrock_anthropic_sse_stream(
        FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0
    ):
        out.append(b)

    # Three SSE events emitted in order
    assert len(out) == 3
    assert out[0].startswith(b"event: message_start\n")
    assert out[1].startswith(b"event: content_block_delta\n")
    assert out[2].startswith(b"event: message_delta\n")
    for chunk in out:
        assert chunk.endswith(b"\n\n")

    # Let any pending tasks settle
    await asyncio.sleep(0)

    assert captured, "on_usage must have been invoked"
    u = captured[-1]
    assert u.input_tokens == 10
    assert u.output_tokens == 7
    assert u.cache_creation_input_tokens == 2
    assert u.cache_read_input_tokens == 3


@pytest.mark.asyncio
async def test_streaming_client_disconnect_triggers_background_drain():
    """Client disconnects after the first chunk — remaining chunks are drained
    in the background and usage is still recorded.

    NOTE: the helper intentionally does NOT poll Starlette ``is_disconnected()``
    (it false-positives on the first call in an ASGI streaming context — see the
    comment in ``streaming.py``). A real client disconnect surfaces as a
    ``CancelledError`` injected at the ``yield`` point, so we reproduce that
    here with ``athrow`` after consuming the first chunk."""
    chunks = _sse_chunks(
        [
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 4}},
            },
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "x"},
            },
            {
                "type": "message_delta",
                "usage": {"output_tokens": 9},
            },
        ]
    )
    captured: list[TokenUsage] = []
    drain_done = asyncio.Event()

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)
        drain_done.set()

    gen = bedrock_anthropic_sse_stream(
        FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0
    )

    # Consume exactly the first chunk, then simulate the client going away.
    out: list[bytes] = []
    out.append(await gen.__anext__())

    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError("client disconnect"))

    # At most one chunk reached the client before the disconnect.
    assert len(out) <= 1

    # Background drain runs through the remaining chunks and records usage.
    await asyncio.wait_for(drain_done.wait(), timeout=2.0)

    assert captured, "background drain must still record usage"
    u = captured[-1]
    assert u.input_tokens == 4
    assert u.output_tokens == 9


@pytest.mark.asyncio
async def test_streaming_mid_stream_error_emits_sse_error_event():
    """Upstream raises mid-iteration — generator must emit `event: error`
    SSE event and exit gracefully (no exception bubbles out)."""

    async def bad_iter() -> AsyncIterator[bytes]:
        yield json.dumps(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}
        ).encode()
        raise RuntimeError("upstream boom")

    out: list[bytes] = []
    async for b in bedrock_anthropic_sse_stream(
        FakeRequest(), bad_iter(), on_usage=None, idle_timeout=5.0
    ):
        out.append(b)

    # First chunk is the normal event, last chunk is the SSE error
    assert len(out) >= 2
    assert out[0].startswith(b"event: message_start\n")
    assert out[-1].startswith(b"event: error\n")
    assert b"upstream boom" in out[-1] or b"stream_error" in out[-1]


@pytest.mark.asyncio
async def test_streaming_idle_timeout_emits_sse_error_event():
    """Chunk arrives slower than idle_timeout → timeout SSE error event."""

    async def slow_iter() -> AsyncIterator[bytes]:
        # First chunk arrives fast
        yield json.dumps(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}
        ).encode()
        # Second never arrives within the idle window
        await asyncio.sleep(1.0)
        yield b'{"type":"message_stop"}'

    out: list[bytes] = []
    async for b in bedrock_anthropic_sse_stream(
        FakeRequest(), slow_iter(), on_usage=None, idle_timeout=0.2
    ):
        out.append(b)

    # Should emit the first chunk then a timeout error event and stop
    assert any(chunk.startswith(b"event: message_start\n") for chunk in out)
    last = out[-1]
    assert last.startswith(b"event: error\n")
    assert b"timeout" in last.lower()


@pytest.mark.asyncio
async def test_streaming_malformed_chunk_passthrough():
    """Non-JSON chunk must not crash — emitted as generic SSE data line."""

    chunks = [
        b"not valid json at all",
        json.dumps({"type": "message_stop"}).encode(),
    ]

    out: list[bytes] = []
    async for b in bedrock_anthropic_sse_stream(
        FakeRequest(), _aiter(chunks), on_usage=None, idle_timeout=5.0
    ):
        out.append(b)

    # Malformed chunk is passed through; message_stop event follows
    assert len(out) == 2
    assert b"not valid json at all" in out[0]
    assert out[1].startswith(b"event: message_stop\n")


@pytest.mark.asyncio
async def test_streaming_no_usage_invokes_on_usage_with_zero():
    """KI-08: If no usage-bearing event arrives (disconnect, missing message_delta),
    on_usage MUST still fire with a zero-valued TokenUsage so cost_recorder can
    settle pre-reserved TPM (refund) and avoid a rate-limit leak."""
    chunks = _sse_chunks(
        [
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
            {"type": "message_stop"},
        ]
    )
    captured: list[TokenUsage] = []

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)

    async for _ in bedrock_anthropic_sse_stream(
        FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0
    ):
        pass

    await asyncio.sleep(0.05)
    assert len(captured) == 1
    u = captured[0]
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.total_tokens == 0


@pytest.mark.asyncio
async def test_streaming_cancelled_at_yield_spawns_drain():
    """Starlette client-disconnect manifests as CancelledError at the yield.
    Helper must spawn background drain + re-raise per asyncio contract."""
    chunks = _sse_chunks(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
            {"type": "message_delta", "usage": {"output_tokens": 9}},
        ]
    )
    captured: list[TokenUsage] = []
    drain_done = asyncio.Event()

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)
        drain_done.set()

    gen = bedrock_anthropic_sse_stream(
        FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0
    )
    await gen.__anext__()

    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError("client disconnect"))

    await asyncio.wait_for(drain_done.wait(), timeout=2.0)
    assert captured[-1].input_tokens == 4
    assert captured[-1].output_tokens == 9


@pytest.mark.asyncio
async def test_streaming_generator_close_spawns_drain():
    """aclose() on the helper raises GeneratorExit at the yield."""
    chunks = _sse_chunks(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 2}}},
            {"type": "message_delta", "usage": {"output_tokens": 5}},
        ]
    )
    captured: list[TokenUsage] = []
    drain_done = asyncio.Event()

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)
        drain_done.set()

    gen = bedrock_anthropic_sse_stream(
        FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0
    )
    await gen.__anext__()

    await gen.aclose()

    await asyncio.wait_for(drain_done.wait(), timeout=2.0)
    assert captured[-1].input_tokens == 2
    assert captured[-1].output_tokens == 5
