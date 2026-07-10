# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for OpenAI-compatible SSE streaming (FR-1.4 parity with FR-1.3).

OpenAI chunks arrive already SSE-formatted (`data: {...}\\n\\n`), so the helper
is a passthrough with the same edge case handling as the Bedrock helper:
- client disconnect: stop, background-drain, still record usage
- idle timeout: emit OpenAI-shaped error chunk and stop
- mid-stream error: emit OpenAI-shaped error chunk and stop
- `[DONE]` sentinel: yielded through; no usage extraction
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from app.schemas.domain import TokenUsage
from app.services.streaming import openai_sse_stream


class FakeRequest:
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


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


@pytest.mark.asyncio
async def test_openai_streaming_happy_path_passthrough_and_usage():
    chunks = [
        _sse({"choices": [{"delta": {"content": "hi"}}]}),
        _sse(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            }
        ),
        b"data: [DONE]\n\n",
    ]
    captured: list[TokenUsage] = []

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)

    out: list[bytes] = []
    async for b in openai_sse_stream(
        FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0
    ):
        out.append(b)

    # Chunks pass through unchanged
    assert out == chunks

    await asyncio.sleep(0)
    assert captured, "on_usage must fire with extracted usage"
    u = captured[-1]
    assert u.input_tokens == 5
    assert u.output_tokens == 7
    assert u.total_tokens == 12


@pytest.mark.asyncio
async def test_openai_streaming_client_disconnect_background_drain():
    """Client disconnect after the first chunk → background drain still records
    usage. The helper does not poll ``is_disconnected()`` (false-positives in an
    ASGI streaming context); a real disconnect arrives as a ``CancelledError`` at
    the ``yield``, reproduced here via ``athrow``."""
    chunks = [
        _sse({"choices": [{"delta": {"content": "x"}}]}),
        _sse(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            }
        ),
        b"data: [DONE]\n\n",
    ]
    drain_done = asyncio.Event()
    captured: list[TokenUsage] = []

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)
        drain_done.set()

    gen = openai_sse_stream(
        FakeRequest(),
        _aiter(chunks),
        on_usage=on_usage,
        idle_timeout=5.0,
    )

    out: list[bytes] = []
    out.append(await gen.__anext__())

    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError("client disconnect"))

    assert len(out) <= 1

    await asyncio.wait_for(drain_done.wait(), timeout=2.0)
    assert captured[-1].input_tokens == 3
    assert captured[-1].output_tokens == 4


@pytest.mark.asyncio
async def test_openai_streaming_mid_stream_error_emits_openai_error_chunk():
    async def bad_iter() -> AsyncIterator[bytes]:
        yield _sse({"choices": [{"delta": {"content": "hi"}}]})
        raise RuntimeError("upstream boom")

    out: list[bytes] = []
    async for b in openai_sse_stream(FakeRequest(), bad_iter(), on_usage=None, idle_timeout=5.0):
        out.append(b)

    assert len(out) >= 2
    assert out[0].startswith(b"data: ")
    assert out[-1].startswith(b"data: ")
    tail = json.loads(out[-1].decode().removeprefix("data: ").strip())
    assert "error" in tail
    assert (
        "upstream boom" in tail["error"].get("message", "")
        or tail["error"].get("type") == "stream_error"
    )


@pytest.mark.asyncio
async def test_openai_streaming_idle_timeout_emits_error_chunk():
    async def slow_iter() -> AsyncIterator[bytes]:
        yield _sse({"choices": [{"delta": {"content": "start"}}]})
        await asyncio.sleep(1.0)
        yield b"data: [DONE]\n\n"

    out: list[bytes] = []
    async for b in openai_sse_stream(FakeRequest(), slow_iter(), on_usage=None, idle_timeout=0.2):
        out.append(b)

    last = out[-1]
    assert last.startswith(b"data: ")
    tail = json.loads(last.decode().removeprefix("data: ").strip())
    assert tail["error"]["type"] == "timeout_error"


@pytest.mark.asyncio
async def test_openai_streaming_done_sentinel_invokes_on_usage_with_zero():
    """KI-08: [DONE] without an upstream `usage` field must still invoke on_usage
    with a zero TokenUsage so pre-reserved TPM gets settled (refunded)."""
    captured: list[TokenUsage] = []

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)

    chunks = [
        _sse({"choices": [{"delta": {"content": "x"}}]}),
        b"data: [DONE]\n\n",
    ]
    async for _ in openai_sse_stream(
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
async def test_openai_streaming_cancelled_at_yield_spawns_drain():
    """External cancellation (Starlette client-disconnect) injects CancelledError
    at the yield point. Helper must spawn background drain so usage is still
    recorded, then re-raise CancelledError per asyncio contract.
    """
    chunks = [
        _sse({"choices": [{"delta": {"content": "a"}}]}),
        _sse({"choices": [{"delta": {"content": "b"}}]}),
        _sse(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            }
        ),
    ]
    captured: list[TokenUsage] = []
    drain_done = asyncio.Event()

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)
        drain_done.set()

    gen = openai_sse_stream(FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0)
    first = await gen.__anext__()
    assert first == chunks[0]

    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError("client disconnect"))

    await asyncio.wait_for(drain_done.wait(), timeout=2.0)
    assert captured[-1].input_tokens == 3
    assert captured[-1].output_tokens == 4


@pytest.mark.asyncio
async def test_openai_streaming_generator_close_spawns_drain():
    """aclose() on the helper raises GeneratorExit at the yield; same recovery
    path as CancelledError — drain must still record usage."""
    chunks = [
        _sse({"choices": [{"delta": {"content": "a"}}]}),
        _sse(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 5, "total_tokens": 7},
            }
        ),
    ]
    captured: list[TokenUsage] = []
    drain_done = asyncio.Event()

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)
        drain_done.set()

    gen = openai_sse_stream(FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0)
    await gen.__anext__()

    await gen.aclose()

    await asyncio.wait_for(drain_done.wait(), timeout=2.0)
    assert captured[-1].input_tokens == 2
    assert captured[-1].output_tokens == 5


@pytest.mark.asyncio
async def test_openai_streaming_multiline_chunk_parses_usage():
    """httpx can deliver multiple SSE frames in one chunk — must still find usage."""
    first = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
    second = (
        b'data: {"choices":[],'
        b'"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}\n\n'
    )
    fused = first + second
    captured: list[TokenUsage] = []

    async def on_usage(u: TokenUsage, first_token_time: float | None = None) -> None:
        captured.append(u)

    async for _ in openai_sse_stream(
        FakeRequest(), _aiter([fused]), on_usage=on_usage, idle_timeout=5.0
    ):
        pass

    await asyncio.sleep(0)
    assert captured[-1].input_tokens == 2
    assert captured[-1].output_tokens == 3
