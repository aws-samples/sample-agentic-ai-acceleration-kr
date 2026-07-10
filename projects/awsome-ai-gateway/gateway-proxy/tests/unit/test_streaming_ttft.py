# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""TTFT(time to first token) 계측 유닛 테스트.

streaming SSE 헬퍼가 첫 콘텐츠 델타 시점의 time.monotonic()을 캡처해
on_usage(usage, first_token_time) 2번째 인자로 넘기는지 검증한다.

주의: time.monotonic 만 패치하면 asyncio.wait_for 내부 clock 호출과 충돌하므로
streaming 모듈의 `time` 참조 전체를 fake 로 교체한다(asyncio 실제 clock 은 무영향).
"""

from __future__ import annotations

import types
from collections.abc import AsyncIterator

import pytest

from app.services.streaming import (
    bedrock_anthropic_sse_stream,
    openai_sse_stream,
    responses_sse_stream,
)


class _FakeClock:
    """monotonic()을 결정적으로 진행시키는 fake."""

    def __init__(self, times: list[float]) -> None:
        self._it = iter(times)
        self._last = 0.0

    def __call__(self) -> float:
        try:
            self._last = next(self._it)
        except StopIteration:
            pass
        return self._last


class _FakeRequest:
    pass


async def _aiter(items: list[bytes]) -> AsyncIterator[bytes]:
    for it in items:
        yield it


@pytest.mark.asyncio
async def test_bedrock_stream_records_ttft_at_first_content_delta(monkeypatch):
    # message_start(메타) → content_block_delta(첫 토큰) → message_delta(usage)
    chunks = [
        b'{"type":"message_start","message":{"usage":{"input_tokens":5}}}',
        b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}',
        b'{"type":"message_delta","usage":{"output_tokens":3}}',
    ]
    # streaming 코드가 monotonic()을 호출하는 시점은 첫 content_block_delta 한 번뿐.
    clock = _FakeClock([100.5, 101.0, 101.0, 101.0, 101.0])
    monkeypatch.setattr(
        "app.services.streaming.time", types.SimpleNamespace(monotonic=clock)
    )

    captured: dict = {}

    async def _on_usage(usage, first_token_time):
        captured["usage"] = usage
        captured["ftt"] = first_token_time

    async for _ in bedrock_anthropic_sse_stream(
        _FakeRequest(), _aiter(chunks), on_usage=_on_usage
    ):
        pass

    assert captured["ftt"] == 100.5  # 첫 content_block_delta 시점
    assert captured["usage"].output_tokens == 3


@pytest.mark.asyncio
async def test_bedrock_stream_ttft_none_when_no_content_delta():
    # usage 이벤트만 있고 content_block_delta 없음 → first_token_time None
    chunks = [
        b'{"type":"message_start","message":{"usage":{"input_tokens":5}}}',
        b'{"type":"message_delta","usage":{"output_tokens":3}}',
    ]

    captured: dict = {}

    async def _on_usage(usage, first_token_time):
        captured["ftt"] = first_token_time

    async for _ in bedrock_anthropic_sse_stream(
        _FakeRequest(), _aiter(chunks), on_usage=_on_usage
    ):
        pass

    assert captured["ftt"] is None


@pytest.mark.asyncio
async def test_openai_stream_records_ttft_at_first_content(monkeypatch):
    chunks = [
        b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"llo"}}],'
        b'"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n',
    ]
    clock = _FakeClock([200.0, 200.25, 201.0, 201.0])
    monkeypatch.setattr(
        "app.services.streaming.time", types.SimpleNamespace(monotonic=clock)
    )

    captured: dict = {}

    async def _on_usage(usage, first_token_time):
        captured["ftt"] = first_token_time
        captured["usage"] = usage

    async for _ in openai_sse_stream(_FakeRequest(), _aiter(chunks), on_usage=_on_usage):
        pass

    assert captured["ftt"] == 200.0  # 첫 delta.content 시점
    assert captured["usage"].output_tokens == 2


@pytest.mark.asyncio
async def test_responses_stream_records_ttft_at_first_output_text_delta(monkeypatch):
    # Mantle Responses API: response.output_text.delta 첫 도착이 TTFT.
    chunks = [
        b'{"type":"response.output_text.delta","delta":"O"}',
        b'{"type":"response.completed","response":{"usage":'
        b'{"input_tokens":4,"output_tokens":6,"total_tokens":10}}}',
    ]
    clock = _FakeClock([300.0, 300.5, 301.0, 301.0])
    monkeypatch.setattr(
        "app.services.streaming.time", types.SimpleNamespace(monotonic=clock)
    )

    captured: dict = {}

    async def _on_usage(usage, first_token_time):
        captured["ftt"] = first_token_time
        captured["usage"] = usage

    async for _ in responses_sse_stream(_FakeRequest(), _aiter(chunks), on_usage=_on_usage):
        pass

    assert captured["ftt"] == 300.0  # 첫 output_text.delta 시점
    assert captured["usage"].output_tokens == 6
