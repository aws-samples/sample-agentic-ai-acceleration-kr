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
    # ⚠️ TTFT 는 첫 콘텐츠 델타를 **담은 청크의 도착 시각**이다(포맷 시각이 아니다).
    #    상류 읽기는 펌프 태스크에서 일어나고 그 태스크가 청크마다 monotonic() 을 찍는다.
    #    그래서 클록은 청크 순서대로 소비된다: message_start=100.5,
    #    content_block_delta=101.0, message_delta=101.5.
    #
    #    이 테스트는 예전에 "monotonic 호출은 첫 델타 한 번뿐" 을 전제로 100.5 를 단정했다.
    #    그 전제는 포맷 시점에 시각을 찍던 구현의 것이고, 그 구현에서는 미리 읽어 둔 청크가
    #    실제 도착보다 늦은 시각을 받아 TTFT 가 부풀려졌다. 도착 시각 쪽이 옳다.
    clock = _FakeClock([100.5, 101.0, 101.5, 102.0, 102.5])
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

    # 첫 content_block_delta 를 담은 청크(2번째)의 도착 시각.
    assert captured["ftt"] == 101.0, (
        f"ftt={captured['ftt']} — 첫 콘텐츠 델타 청크의 도착 시각이어야 한다"
    )
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


@pytest.mark.asyncio
async def test_read_ahead_does_not_inflate_ttft(monkeypatch):
    """⚠️ 이 성질이 도착 시각으로 찍는 이유다.

    상류가 프레임 루프보다 빠르면 펌프가 여러 청크를 미리 읽어 큐에 넣는다. 그때 포맷
    시점의 시각을 쓰면 TTFT 가 "큐에서 꺼낸 시각" 이 되어, 실제 첫 토큰 도착보다 늦게
    측정된다 — 느린 클라이언트일수록 더 부풀려지고, 그 값은 SLO 지표로 쓰인다.

    상류를 한 번에 다 내보내고 프레임 루프를 늦게 소비하게 만들어, TTFT 가 **앞쪽**
    클록값을 잡는지 본다.
    """
    import asyncio

    chunks = [
        b'{"type":"message_start","message":{"usage":{"input_tokens":5}}}',
        b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}',
        b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"there"}}',
        b'{"type":"message_delta","usage":{"output_tokens":3}}',
    ]
    # 도착은 10,11,12,13 / 소비는 그 뒤(50+). 포맷 시점을 쓰면 50 이상이 잡힌다.
    clock = _FakeClock([10.0, 11.0, 12.0, 13.0, 50.0, 51.0, 52.0, 53.0, 54.0])
    monkeypatch.setattr(
        "app.services.streaming.time", types.SimpleNamespace(monotonic=clock)
    )

    captured: dict = {}

    async def _on_usage(usage, first_token_time):
        captured["ftt"] = first_token_time

    async def _burst() -> AsyncIterator[bytes]:
        for c in chunks:
            yield c

    async for _ in bedrock_anthropic_sse_stream(
        _FakeRequest(), _burst(), on_usage=_on_usage
    ):
        # 프레임 루프를 일부러 늦춘다 → 펌프가 앞서 읽는다.
        await asyncio.sleep(0)

    assert captured["ftt"] is not None
    assert captured["ftt"] <= 13.0, (
        f"ftt={captured['ftt']} — 도착 시각이 아니라 소비/포맷 시각을 잡았다"
    )
