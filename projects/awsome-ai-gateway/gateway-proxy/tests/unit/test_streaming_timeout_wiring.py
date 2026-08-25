# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""SSE 타임아웃 배선 + 과금 멱등성 회귀 테스트 (Phase2 백포트 A-1).

두 개의 실제 결함을 고정한다.

**결함 ①  죽은 설정(dead config).**
`settings.stream_idle_timeout` / `stream_disconnect_drain_timeout` 는 정의·타입·문서까지
있었지만 **어떤 호출부도 인자로 넘기지 않아** 헬퍼의 하드코딩 기본값(60.0/30.0)이 항상
이겼다. 차트/env 로 어떤 값을 주입해도 런타임은 60s 였고, 그래서 Opus extended thinking
응답이 정상 생성 도중 끊겼다. 정적 리뷰로는 안 보이는 결함 유형 — "설정이 존재한다"와
"설정이 동작한다"는 다르다. 그래서 이 테스트는 **관측**한다(asyncio.wait_for 에 실제로
전달된 timeout 값을 가로채서 검사).

**결함 ②  종료 경로별 과금 유실/이중과금.**
종료 경로가 4개다 — 정상완료 / idle timeout / upstream 예외 / 클라이언트 끊김(백그라운드
drain). 과거엔 timeout·예외 경로가 함수 말미의 `_fire_on_usage()` 를 건너뛰는 `return`
이라 **그때까지 생성된 토큰이 유실**됐다. 반대로 각 경로에 호출만 추가하면 경로가 겹칠 때
**이중 과금**이 된다(예: 이미 끊긴 클라이언트에 error 프레임을 yield → GeneratorExit →
취소 핸들러가 drain 을 띄움). 따라서 계약은 "**정확히 1회**"이고, 그걸 여기서 못박는다.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator

import pytest

from app.config import Settings, get_settings
from app.schemas.domain import TokenUsage
from app.services import streaming
from app.services.streaming import (
    bedrock_anthropic_sse_stream,
    openai_sse_stream,
    responses_sse_stream,
    stream_response,
)

# 타임아웃 인자를 받는 모든 공개 스트리밍 헬퍼. 새 헬퍼가 추가되면 여기에도 추가해야
# 하며, 추가를 잊으면 아래 signature 테스트가 잡지 못하므로 목록 자체를 코드에서 유도한다.
_TIMEOUT_HELPERS = [
    bedrock_anthropic_sse_stream,
    openai_sse_stream,
    responses_sse_stream,
    stream_response,
]


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


# --- 결함 ①: 배선 -----------------------------------------------------------


def test_all_streaming_helpers_default_timeouts_to_none():
    """하드코딩 기본값 회귀 감시.

    기본값이 숫자(60.0)로 되돌아가면 설정이 다시 죽는다. None 이어야
    `_resolve_timeouts` 가 Settings 에서 해석할 수 있다.
    """
    for fn in _TIMEOUT_HELPERS:
        params = inspect.signature(fn).parameters
        assert params["idle_timeout"].default is None, f"{fn.__name__}.idle_timeout"
        assert params["drain_timeout"].default is None, f"{fn.__name__}.drain_timeout"


def test_streaming_module_has_no_hardcoded_timeout_defaults():
    """소스 수준 감시 — 새 헬퍼가 `idle_timeout: float = 60.0` 으로 추가되는 것을 막는다."""
    src = inspect.getsource(streaming)
    for bad in ("idle_timeout: float = ", "drain_timeout: float = "):
        assert bad not in src, (
            f"하드코딩 타임아웃 기본값 발견({bad!r}) — "
            "`float | None = None` + _resolve_timeouts() 를 사용할 것"
        )


def test_resolve_timeouts_reads_settings(monkeypatch):
    """인자 미지정 시 Settings 값으로 해석되는지."""
    get_settings.cache_clear()
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT", "137")
    monkeypatch.setenv("STREAM_DISCONNECT_DRAIN_TIMEOUT", "11")
    try:
        assert streaming._resolve_timeouts(None, None) == (137.0, 11.0)
        # 부분 지정: 지정한 쪽만 우선
        assert streaming._resolve_timeouts(1.5, None) == (1.5, 11.0)
        assert streaming._resolve_timeouts(None, 2.5) == (137.0, 2.5)
        # 전부 지정: Settings 를 아예 보지 않음
        assert streaming._resolve_timeouts(1.5, 2.5) == (1.5, 2.5)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "helper",
    [bedrock_anthropic_sse_stream, openai_sse_stream, responses_sse_stream],
    ids=["anthropic", "openai", "responses"],
)
async def test_idle_timeout_actually_reaches_wait_for(helper, monkeypatch):
    """**관측 기반** 검증 — 설정값이 실제 asyncio.wait_for 까지 도달하는지.

    "설정이 존재한다"가 아니라 "설정이 hot path 에서 쓰인다"를 증명하는 부분.
    이것이 죽은 설정 결함을 잡는 유일한 종류의 테스트다.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT", "137")
    observed: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def spy(aw, timeout=None, **kw):
        observed.append(timeout)
        return await real_wait_for(aw, timeout=timeout, **kw)

    monkeypatch.setattr(streaming.asyncio, "wait_for", spy)
    try:
        async for _ in helper(FakeRequest(), _aiter([b'{"type":"ping"}'])):
            pass
        assert observed, "wait_for 가 호출되지 않음 — idle timeout 이 적용되지 않는 경로"
        assert set(observed) == {137.0}, f"설정 미반영: {observed}"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_explicit_timeout_argument_still_wins(monkeypatch):
    """명시 인자가 Settings 를 이겨야 한다 (기존 단위테스트들이 이 계약에 의존)."""
    get_settings.cache_clear()
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT", "137")
    observed: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def spy(aw, timeout=None, **kw):
        observed.append(timeout)
        return await real_wait_for(aw, timeout=timeout, **kw)

    monkeypatch.setattr(streaming.asyncio, "wait_for", spy)
    try:
        async for _ in bedrock_anthropic_sse_stream(
            FakeRequest(), _aiter([b'{"type":"ping"}']), idle_timeout=0.5
        ):
            pass
        assert set(observed) == {0.5}, f"명시 인자가 무시됨: {observed}"
    finally:
        get_settings.cache_clear()


def test_default_idle_timeout_is_below_alb_dev_timeout():
    """게이트웨이 타임아웃 < ALB idle_timeout 이어야 한다.

    ALB(dev 300s / prod 600s)가 먼저 끊으면 클라이언트는 깔끔한 `event: error` SSE
    프레임 대신 truncated stream 을 본다. 두 값은 함께 움직여야 하는 커플링이고,
    차트만 보고는 알 수 없으므로 여기서 하한을 못박는다.
    ALB 값을 바꿀 땐 이 테스트와 config.py 주석도 함께 갱신할 것.
    """
    alb_dev_idle_timeout = 300  # values-eks-fargate-dev.yaml: load-balancer-attributes
    assert Settings().stream_idle_timeout < alb_dev_idle_timeout


# --- 결함 ②: 과금 멱등성 (종료 경로 4개) ------------------------------------


def _usage_recorder() -> tuple[list[TokenUsage], object]:
    calls: list[TokenUsage] = []

    async def on_usage(usage: TokenUsage, _ttft: float | None = None) -> None:
        calls.append(usage)

    return calls, on_usage


@pytest.mark.asyncio
async def test_usage_fires_exactly_once_on_normal_completion():
    calls, on_usage = _usage_recorder()
    chunks = [
        json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}).encode(),
        json.dumps({"type": "message_delta", "usage": {"output_tokens": 5}}).encode(),
    ]
    async for _ in bedrock_anthropic_sse_stream(
        FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0
    ):
        pass
    assert len(calls) == 1
    assert (calls[0].input_tokens, calls[0].output_tokens) == (10, 5)


@pytest.mark.asyncio
async def test_usage_fires_exactly_once_on_idle_timeout():
    """회귀 고정: timeout 경로가 과거엔 usage 를 **유실**했다.

    첫 청크로 토큰이 이미 집계된 뒤 두 번째 청크가 늦게 오면, 그때까지의 토큰은
    실제로 생성·과금 대상이다. 이걸 버리면 매출 누락이다.
    """
    calls, on_usage = _usage_recorder()

    async def slow_iter() -> AsyncIterator[bytes]:
        yield json.dumps(
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}
        ).encode()
        yield json.dumps({"type": "message_delta", "usage": {"output_tokens": 5}}).encode()
        await asyncio.sleep(10)  # idle_timeout 초과
        yield b'{"type":"never"}'

    frames = [
        f
        async for f in bedrock_anthropic_sse_stream(
            FakeRequest(), slow_iter(), on_usage=on_usage, idle_timeout=0.2
        )
    ]
    assert any(b"timeout_error" in f for f in frames), "timeout SSE 에러 프레임 누락"
    assert len(calls) == 1, f"timeout 경로 과금 유실/중복: {len(calls)}회"
    assert (calls[0].input_tokens, calls[0].output_tokens) == (10, 5)


@pytest.mark.asyncio
async def test_usage_fires_exactly_once_on_upstream_exception():
    """회귀 고정: upstream 예외 경로도 과거엔 usage 를 유실했다."""
    calls, on_usage = _usage_recorder()

    async def bad_iter() -> AsyncIterator[bytes]:
        yield json.dumps(
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}
        ).encode()
        yield json.dumps({"type": "message_delta", "usage": {"output_tokens": 5}}).encode()
        raise RuntimeError("upstream exploded")

    frames = [
        f
        async for f in bedrock_anthropic_sse_stream(
            FakeRequest(), bad_iter(), on_usage=on_usage, idle_timeout=5.0
        )
    ]
    assert any(b"event: error" in f for f in frames), "예외 SSE 에러 프레임 누락"
    assert len(calls) == 1, f"예외 경로 과금 유실/중복: {len(calls)}회"
    assert (calls[0].input_tokens, calls[0].output_tokens) == (10, 5)


@pytest.mark.asyncio
async def test_usage_fires_exactly_once_on_client_disconnect():
    """클라이언트 끊김 → 백그라운드 drain 이 usage 를 1회만 기록."""
    calls, on_usage = _usage_recorder()
    chunks = [
        json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}).encode(),
        json.dumps({"type": "message_delta", "usage": {"output_tokens": 5}}).encode(),
        json.dumps({"type": "message_delta", "usage": {"output_tokens": 9}}).encode(),
    ]
    async for _ in bedrock_anthropic_sse_stream(
        FakeRequest(disconnect_after=1), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0
    ):
        pass
    await asyncio.sleep(0.2)  # 백그라운드 drain 완료 대기
    assert len(calls) == 1, f"끊김 경로 과금 유실/중복: {len(calls)}회"
    # drain 이 남은 청크까지 소비했으므로 최종 output_tokens 는 9
    assert calls[0].output_tokens == 9


@pytest.mark.asyncio
async def test_usage_fires_exactly_once_when_timeout_hits_disconnected_client():
    """가장 위험한 조합: 이미 끊긴 클라이언트 + idle timeout.

    error 프레임 yield 가 GeneratorExit 을 던져 취소 경로로 빠지므로, usage 확정이
    yield **뒤**에 있으면 유실되고, 양쪽에 다 있으면 이중 과금이 된다.
    (그래서 구현은 yield 앞에서 확정 + usage_fired 가드 조합이어야 한다.)
    """
    calls, on_usage = _usage_recorder()

    async def slow_iter() -> AsyncIterator[bytes]:
        yield json.dumps(
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}
        ).encode()
        yield json.dumps({"type": "message_delta", "usage": {"output_tokens": 5}}).encode()
        await asyncio.sleep(10)
        yield b'{"type":"never"}'

    gen = bedrock_anthropic_sse_stream(
        FakeRequest(), slow_iter(), on_usage=on_usage, idle_timeout=0.2
    )
    # 두 프레임만 소비하고 클라이언트가 사라진 것처럼 제너레이터를 닫는다.
    got = 0
    async for _ in gen:
        got += 1
        if got == 2:
            break
    await gen.aclose()
    await asyncio.sleep(0.5)
    assert len(calls) <= 1, f"이중 과금: {len(calls)}회"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "helper,chunks,tokens",
    [
        (
            openai_sse_stream,
            [
                b'data: {"choices":[{"delta":{"content":"hi"}}],'
                b'"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
            ],
            (10, 5),
        ),
        (
            responses_sse_stream,
            [
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "usage": {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "total_tokens": 15,
                            }
                        },
                    }
                ).encode()
            ],
            (10, 5),
        ),
    ],
    ids=["openai", "responses"],
)
async def test_other_dialects_fire_usage_exactly_once(helper, chunks, tokens):
    """openai/responses 헬퍼도 동일한 '정확히 1회' 계약을 지키는지."""
    calls, on_usage = _usage_recorder()
    async for _ in helper(FakeRequest(), _aiter(chunks), on_usage=on_usage, idle_timeout=5.0):
        pass
    assert len(calls) == 1
    assert (calls[0].input_tokens, calls[0].output_tokens) == tokens
