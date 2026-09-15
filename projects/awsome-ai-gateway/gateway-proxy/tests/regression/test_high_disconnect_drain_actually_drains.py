# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""클라이언트가 끊긴 뒤의 배수(drain)가 **정말로 청크를 받는지**.

무엇이 문제였나
---------------
프레임 루프는 거의 모든 시간을 ``await asyncio.wait_for(iterator.__anext__(), ...)`` 에서
파킹된 상태로 보낸다(모델이 생각하는 동안). 클라이언트가 그 시점에 끊으면 Starlette 이
응답 태스크를 취소하고, ``CancelledError`` 가 **상류 제너레이터 안으로** 전달되어 그것을
닫는다. 그래서 과금을 지키려고 띄우는 배수 태스크는 이미 끝난 제너레이터를 순회하며
**한 청크도 얻지 못했다** — ``stream_disconnect_drain_timeout`` 은 실질적으로 죽은 설정이었다.

무엇을 잃었나
-------------
  Anthropic  최종 ``output_tokens`` 를 담은 ``message_delta`` 와
             ``amazon-bedrock-invocationMetrics`` 는 스트림 **끝**에 온다 → output_tokens 가
             0 이 되고 과금이 tokenizer 추정치로 떨어진다(estimated=True 행).
  Responses  usage 가 종결 이벤트 **안에만** 있다 → usage 전체가 0 이고
             ``cost_recorder.finalize`` 가 usage_logs 행을 **아예 만들지 않는다.**

가장 흔한 경로가 하필 이것이다: Claude Code 사용자가 thinking 중에 Esc 를 누르는 것.

⚠️ 이 파일은 실제 취소를 일으켜 검증한다. "배수 함수가 존재한다" 나 "취소 시 태스크를
   띄운다" 로는 잡히지 않는다 — 옛 코드도 그 둘을 만족했고 배수 결과만 0 이었다.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.streaming import (
    _UpstreamPump,
    bedrock_anthropic_sse_stream,
    responses_sse_stream,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


class _Req:
    async def is_disconnected(self) -> bool:
        return False


def _raw(ev: dict) -> bytes:
    return json.dumps(ev).encode()


# ─────────────────────────────────────────────────────────────────────────────
# 1. 펌프 자체 — 프레임 루프 취소가 상류를 닫지 않는다
# ─────────────────────────────────────────────────────────────────────────────


async def test_cancelling_the_reader_does_not_close_the_upstream():
    """⚠️ 이 파일의 근본 성질.

    ``pump.next()`` 를 기다리다 취소돼도 상류 제너레이터는 살아 있어야 한다. 예전에는
    ``iterator.__anext__()`` 를 직접 감쌌기 때문에 취소가 제너레이터 안으로 전달됐다.
    """
    closed = {"hit": False}

    async def upstream() -> AsyncIterator[bytes]:
        try:
            for i in range(5):
                await asyncio.sleep(0.01)
                yield f"c{i}".encode()
        finally:
            closed["hit"] = True

    pump = _UpstreamPump(upstream(), label="test")
    first, _t = await pump.next(timeout=1.0)
    assert first == b"c0"

    # 프레임 루프의 대기를 취소한다.
    task = asyncio.create_task(pump.next(timeout=1.0))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 상류는 계속 읽혀야 한다 — 남은 청크를 배수로 받을 수 있다.
    got = [c async for c in pump.drain(timeout=1.0)]
    assert got, "취소가 상류를 닫았다 — 배수가 아무것도 받지 못한다"
    assert b"c4" in got, f"마지막 청크를 받지 못했다: {got}"
    pump.close()


async def test_the_pump_forwards_upstream_exceptions():
    """상류 예외를 삼키면 프레임 루프가 영원히 기다린다."""

    async def boom() -> AsyncIterator[bytes]:
        yield b"c0"
        raise RuntimeError("upstream died")

    pump = _UpstreamPump(boom(), label="test")
    assert (await pump.next(timeout=1.0))[0] == b"c0"
    with pytest.raises(RuntimeError, match="upstream died"):
        await pump.next(timeout=1.0)
    pump.close()


async def test_the_pump_signals_eof_distinctly_from_an_empty_chunk():
    """빈 청크와 EOF 를 구별해야 한다 — 어댑터가 빈 바이트를 흘릴 수 있다."""

    async def with_empty() -> AsyncIterator[bytes]:
        yield b""
        yield b"c1"

    pump = _UpstreamPump(with_empty(), label="test")
    assert (await pump.next(timeout=1.0))[0] == b""
    assert (await pump.next(timeout=1.0))[0] == b"c1"
    with pytest.raises(StopAsyncIteration):
        await pump.next(timeout=1.0)
    pump.close()


async def test_the_read_ahead_queue_is_bounded():
    """무제한이면 느린 클라이언트 하나가 상류 전체를 메모리에 담는다."""
    produced = {"n": 0}

    async def infinite() -> AsyncIterator[bytes]:
        while True:
            produced["n"] += 1
            yield b"x" * 16
            await asyncio.sleep(0)

    pump = _UpstreamPump(infinite(), read_ahead=4, label="test")
    await asyncio.sleep(0.05)  # 펌프가 마음껏 달리게 둔다
    # 큐 4 + 넣으려고 대기 중 1 정도. 상한이 없으면 수천 개가 된다.
    assert produced["n"] <= 12, f"{produced['n']}개나 미리 읽었다 — 배압이 없다"
    pump.close()


async def test_close_does_not_leave_a_dangling_task():
    async def slow() -> AsyncIterator[bytes]:
        await asyncio.sleep(10)
        yield b"never"

    pump = _UpstreamPump(slow(), label="test")
    pump.close()
    await asyncio.sleep(0)
    assert pump._task.cancelled() or pump._task.done()


# ─────────────────────────────────────────────────────────────────────────────
# 2. 실제 스트림 — 끊긴 뒤 usage 가 보존되는가
# ─────────────────────────────────────────────────────────────────────────────


async def _disconnect_after_first_frame(helper, chunks, *, gap: float = 0.02):
    """첫 프레임을 받은 뒤 소비 태스크를 취소하고, 배수가 남긴 usage 를 돌려준다."""
    seen: list = []

    async def on_usage(usage, _ttft=None):
        seen.append(usage)

    async def upstream() -> AsyncIterator[bytes]:
        for c in chunks:
            await asyncio.sleep(gap)
            yield c

    gen = helper(_Req(), upstream(), on_usage=on_usage, drain_timeout=2.0)

    async def consume():
        async for _ in gen:
            # 첫 프레임을 받은 직후 상류를 기다리는 상태로 들어간다 — 그 지점이
            # 실제로 가장 흔한 끊김 지점이다.
            await asyncio.sleep(gap * 3)

    task = asyncio.create_task(consume())
    await asyncio.sleep(gap * 1.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # ⚠️ 소비 태스크를 취소하는 것만으로는 부족하다. 취소는 소비자 쪽 await 에서 일어나고,
    #    제너레이터는 `yield` 에 **그대로 매달린 채** 남는다 — 배수를 띄우는 except 절은
    #    제너레이터가 **닫힐 때** 비로소 실행된다. ASGI 서버(Starlette)는 클라이언트가
    #    끊기면 응답 이터레이터를 닫으므로, 그것을 재현하려면 aclose() 를 불러야 한다.
    #    (이 줄이 없으면 배수가 GC 시점까지 늦어져 테스트가 "발화 안 함" 으로 오판한다 —
    #     실제로 그렇게 한 번 틀렸다.)
    await gen.aclose()

    # 배수가 끝나기를 기다린다.
    for _ in range(80):
        if seen:
            break
        await asyncio.sleep(0.02)
    return seen


async def test_anthropic_output_tokens_survive_a_disconnect():
    """⚠️ 최종 output_tokens 는 스트림 **끝**의 message_delta 에만 있다.

    배수가 비면 output_tokens=0 이 되고 과금이 추정치로 떨어진다.
    """
    chunks = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 11}}}),
        _raw({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}),
        _raw({"type": "message_delta", "usage": {"output_tokens": 77}}),
    ]
    seen = await _disconnect_after_first_frame(bedrock_anthropic_sse_stream, chunks)
    assert seen, "on_usage 가 발화하지 않았다"
    usage = seen[0]
    assert usage.output_tokens == 77, (
        f"output_tokens={usage.output_tokens} — 배수가 끝 프레임을 받지 못했다"
    )
    assert usage.input_tokens == 11


async def test_anthropic_invocation_metrics_survive_a_disconnect():
    """Bedrock 이 최종 집계한 billable 토큰도 끝 프레임에 온다."""
    chunks = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}),
        _raw({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}),
        _raw(
            {
                "amazon-bedrock-invocationMetrics": {
                    "inputTokenCount": 21,
                    "outputTokenCount": 34,
                }
            }
        ),
    ]
    seen = await _disconnect_after_first_frame(bedrock_anthropic_sse_stream, chunks)
    assert seen
    assert seen[0].output_tokens == 34, f"invocationMetrics 를 놓쳤다: {seen[0]}"


async def test_responses_usage_survives_a_disconnect():
    """⚠️ 이 방언이 가장 비싸다 — usage 가 종결 이벤트 안에만 있어서, 배수가 비면
    usage 가 전부 0 이고 usage_logs 행이 **아예 만들어지지 않는다.**
    """
    chunks = [
        _raw({"type": "response.created", "response": {"id": "r1"}}),
        _raw({"type": "response.output_text.delta", "output_index": 0, "delta": "hi"}),
        _raw(
            {
                "type": "response.completed",
                "response": {
                    "id": "r1",
                    "status": "completed",
                    "usage": {"input_tokens": 13, "output_tokens": 41},
                },
            }
        ),
    ]
    seen = await _disconnect_after_first_frame(responses_sse_stream, chunks)
    assert seen, "on_usage 가 발화하지 않았다"
    assert seen[0].output_tokens == 41, (
        f"output_tokens={seen[0].output_tokens} — 종결 이벤트를 배수로 받지 못했다. "
        "이 상태에서 cost_recorder 는 usage_logs 행을 만들지 않는다."
    )


async def test_usage_still_fires_exactly_once_on_disconnect():
    """⚠️ 배수가 실제로 동작하게 되면서 발화 지점이 늘었다 — 정확히 1회를 유지해야 한다.

    두 번 발화하면 예산이 두 번 차감된다.
    """
    chunks = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}),
        _raw({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}),
        _raw({"type": "message_delta", "usage": {"output_tokens": 9}}),
    ]
    seen = await _disconnect_after_first_frame(bedrock_anthropic_sse_stream, chunks)
    await asyncio.sleep(0.1)
    assert len(seen) == 1, f"on_usage 가 {len(seen)}회 발화 — 예산이 중복 차감된다"


async def test_normal_completion_is_unaffected():
    """⚠️ 대조군. 위 단정들이 "항상 배수" 로 통과하는 것이 아님을 보인다."""
    seen: list = []

    async def on_usage(usage, _ttft=None):
        seen.append(usage)

    async def upstream() -> AsyncIterator[bytes]:
        for c in (
            _raw({"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
            _raw({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "y"}}),
            _raw({"type": "message_delta", "usage": {"output_tokens": 4}}),
        ):
            yield c

    frames = [
        f
        async for f in bedrock_anthropic_sse_stream(_Req(), upstream(), on_usage=on_usage)
    ]
    assert len(frames) == 3
    assert len(seen) == 1 and seen[0].output_tokens == 4


# ─────────────────────────────────────────────────────────────────────────────
# 3. 구조 — 직접 순회로 되돌아가지 않았는지
# ─────────────────────────────────────────────────────────────────────────────


def test_no_helper_reads_the_upstream_iterator_directly():
    """⚠️ ``iterator.__anext__()`` 나 ``async for chunk in iterator`` 로 되돌아가면
    배수가 다시 0 청크가 된다 — 그리고 그 회귀는 아무 오류도 내지 않는다.
    """
    src = (_SRC / "services" / "streaming.py").read_text(encoding="utf-8")
    tree_for_code = ast.parse(src)
    # ⚠️ **docstring 을 벗긴다.** 이 파일의 펌프 docstring 이 옛 코드 모양
    #    (``iterator.__anext__()``)을 설명으로 인용하기 때문에, 그러지 않으면 검사가 자기
    #    문서를 위반으로 읽는다(실제로 그렇게 한 번 틀렸다). 테스트는 실행되는 것만 봐야 한다.
    for node in ast.walk(tree_for_code):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = ""
    code = ast.dump(tree_for_code)
    assert "iterator.__anext__" not in code, (
        "프레임 루프가 상류 iterator 를 직접 읽는다 — 취소가 상류를 닫는다"
    )
    # 펌프 내부(_run)의 순회는 정당하다. 그 밖에서는 없어야 한다.
    tree = ast.parse(src)
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name == "_run":
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.AsyncFor)
                and isinstance(node.iter, ast.Attribute)
                and node.iter.attr == "_iterator"
            ):
                offenders.append(f"{fn.name}:L{node.lineno}")
    assert offenders == [], f"펌프 밖에서 상류를 직접 순회한다: {offenders}"


def test_all_three_helpers_use_the_pump():
    src = (_SRC / "services" / "streaming.py").read_text(encoding="utf-8")
    assert src.count('_UpstreamPump(chunk_iter') == 3, (
        "세 헬퍼 모두 펌프를 써야 한다 — 한쪽만 고치면 그 방언만 계속 과금을 잃는다"
    )
    assert src.count("pump.drain(timeout=drain_timeout)") == 3
    assert src.count("pump.next(timeout=idle_timeout)") == 3


def test_the_drain_closes_the_pump():
    """배수가 끝난 뒤 펌프 태스크를 남기면 스트림당 태스크가 누적된다."""
    src = (_SRC / "services" / "streaming.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    drains = [
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "_drain_remaining"
    ]
    assert len(drains) == 3, f"_drain_remaining 이 {len(drains)}개"
    for fn in drains:
        closes = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "close"
        ]
        assert closes, f"L{fn.lineno}: 배수가 pump.close() 를 부르지 않는다"
