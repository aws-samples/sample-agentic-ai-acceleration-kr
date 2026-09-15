# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""웹서치 루프의 남은 다섯 결함 — 삼켜지는 도구 호출, 새어 나가는 배관, 없는 봉투.

각각 독립적이지만 모두 "게이트웨이가 클라이언트에게 거짓말을 한다" 는 같은 모양이다.

#6 클라이언트 도구 호출이 삼켜졌다
   ``is_search_turn = pending_searches and not client_tool_present`` 로 루프를 계속할지
   정한다. 그런데 ``client_tool_present`` 는 Anthropic 은 정확히 ``tool_use``, Responses 는
   정확히 ``function_call`` 만 셌다. 두 방언 모두 그것이 도구 호출 항목의 전부가 아니다 —
   Responses 는 커스텀(freeform) 도구를 ``custom_tool_call``, 로컬 실행 도구를
   ``local_shell_call`` / ``computer_call`` 로 보낸다.

   모델이 우리 web_search 와 클라이언트 도구를 **같은 턴**에 호출하면(두 방언 모두 병렬
   도구 호출을 지원한다) 게이트웨이는 검색만 처리하고 루프를 한 바퀴 더 돈다. 클라이언트의
   도구 호출은 그 자리에서 사라진다 — 클라이언트는 자기가 실행해야 할 호출을 보지 못한 채
   기다린다.

#5 우리 배관이 비스트리밍 응답 본문으로 새어 나갔다
   클라이언트 도구 호출이 있는 턴은 terminal 이라 상류 본문이 그대로 반환된다. 그 본문에
   우리 ``web_search`` tool_use 블록(Responses 는 function_call 항목)이 들어 있다.
   클라이언트는 선언하지도 않은 도구 호출을 받고, Anthropic 계약상 모든 tool_use 는
   tool_result 로 답해야 하므로 다음 턴에서 400 을 받거나 알 수 없는 도구로 보고한다.
   스트리밍 경로는 이 블록들을 이미 억제한다 — 비스트리밍만 빠져 있었다.

#4 봉투를 열지 않고 종료 프레임을 지어냈다
   상류가 200 을 주고 ``message_start`` 없이 오류 이벤트만 보내면, 스티처는
   ``message_stop`` 을 내보냈다. Anthropic SSE 계약은 message_start → … → message_stop 이고
   start 없는 stop 은 SDK 파싱 오류다 — 상류 오류가 게이트웨이 버그로 보인다.
   ``_drain_error`` 와 except 절은 이 구분을 하는데 정상 종료 블록만 빠져 있었다.

#3 종료 프레임의 usage 에 입력 토큰이 없었다
   ``message_start`` 는 첫 턴에 한 번만 나가므로 그 프레임의 ``input_tokens`` 는 1턴치다.
   N 턴을 돈 요청에서 클라이언트(Claude Code 는 이 값으로 컨텍스트를 추적한다)는 실제로
   소비한 입력의 일부만 본다 — 우리가 청구하는 양과도 어긋난다.

#1 첫 턴이 실패하면 예약이 영구히 잡혀 있었다
   비스트리밍 루프의 ``finally`` 는 ``토큰 > 0`` 일 때만 ``on_usage`` 를 불렀다. 그 콜백이
   ``cost_recorder.finalize`` 이고, 그 zero-usage 경로가 이 라우트에서 RPM/TPM/비용 예약을
   되돌리는 **유일한** 지점이다(웹서치 분기는 폴백 루프를 타지 않으므로
   ``release_reservations`` 도 돌지 않는다). 400 을 받은 요청이 한 푼도 쓰지 않고 사용자의
   분/시간 한도를 창이 끝날 때까지 물고 있었다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import app.services.web_search_loop as wsl


def _raw(ev: dict) -> bytes:
    return json.dumps(ev).encode()


class _Mcp:
    def __init__(self, text: str = '{"results":[{"t":"WEBTEXT"}]}'):
        self.text = text
        self.calls = 0

    async def ensure_initialized(self) -> str:
        return "WebSearch"

    async def search(self, query, max_results):
        self.calls += 1

        class _R:
            raw_text = self.text

        return _R()


def _events(out: list[str]) -> list[str]:
    return [c.split("\n", 1)[0].replace("event: ", "") for c in out]


def _payloads(out: list[str], event: str) -> list[dict]:
    got = []
    for chunk in out:
        head, _, rest = chunk.partition("\n")
        if head.replace("event: ", "") != event:
            continue
        got.append(json.loads(rest.partition("data: ")[2]))
    return got


# ─────────────────────────────────────────────────────────────────────────────
# 판정 헬퍼 — 화이트리스트가 아니라 접미사로
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "itype",
    ["function_call", "custom_tool_call", "local_shell_call", "computer_call", "mcp_call"],
)
def test_every_responses_tool_call_shape_counts_as_client_owned(itype: str):
    """⚠️ 새 호출 유형이 생겼을 때 **삼키는 쪽이 아니라 넘겨주는 쪽**으로 틀려야 한다.

    삼키면 클라이언트가 영구히 기다린다. 한 턴 일찍 끝나는 것은 회복 가능하다.
    """
    assert wsl._is_client_tool_call_item({"type": itype, "name": "shell"})


def test_our_own_call_is_not_client_owned():
    assert not wsl._is_client_tool_call_item(
        {"type": "function_call", "name": wsl.GW_WEB_SEARCH_NAME}
    )
    # call_id 로도 알아본다 — 이름이 빠진 항목이 있다.
    assert not wsl._is_client_tool_call_item({"type": "function_call", "call_id": "c1"}, {"c1"})


def test_non_call_items_are_not_tool_calls():
    """⚠️ 대조군. 접미사 판정이 지나치게 넓으면 텍스트 턴마다 루프가 끝난다."""
    for itype in ("message", "reasoning", "function_call_output", "text"):
        assert not wsl._is_client_tool_call_item({"type": itype}), itype


@pytest.mark.parametrize("btype", ["tool_use", "server_tool_use", "mcp_tool_use"])
def test_every_anthropic_tool_use_shape_counts_as_client_owned(btype: str):
    assert wsl._is_client_tool_use_block({"type": btype, "name": "Bash"})


def test_our_anthropic_block_is_not_client_owned():
    assert not wsl._is_client_tool_use_block(
        {"type": "tool_use", "name": wsl.GW_WEB_SEARCH_NAME}
    )
    for btype in ("text", "thinking", "tool_result"):
        assert not wsl._is_client_tool_use_block({"type": btype, "name": "x"}), btype


# ─────────────────────────────────────────────────────────────────────────────
# #6 — 병렬 호출 턴에서 클라이언트 도구가 삼켜지지 않는다 (Responses 스트리밍)
# ─────────────────────────────────────────────────────────────────────────────


async def _run_responses(turns, **kw):
    queue = list(turns)
    mcp = kw.pop("mcp", None) or _Mcp()

    async def invoke_stream(body):
        frames = queue.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(_u):
        pass

    out: list[str] = []
    async for chunk in wsl._responses_stream(
        invoke_stream=invoke_stream,
        base_body={"input": "hi"},
        mcp_client=mcp,
        request=None,
        on_usage=on_usage,
        max_iterations=3,
        deadline=asyncio.get_event_loop().time() + 999,
        default_max_results=5,
        **kw,
    ):
        out.append(chunk.decode())
    return out, mcp, queue


def _responses_parallel_turn() -> list[bytes]:
    """우리 web_search 와 클라이언트의 custom_tool_call 이 **같은 턴**에 온다."""
    return [
        _raw({"type": "response.created", "response": {"id": "resp_1"}}),
        _raw({"type": "response.output_item.added", "output_index": 0,
              "item": {"type": "function_call", "call_id": "c_ours",
                       "name": wsl.GW_WEB_SEARCH_NAME}}),
        _raw({"type": "response.function_call_arguments.delta", "output_index": 0,
              "delta": '{"query":"q"}'}),
        _raw({"type": "response.output_item.done", "output_index": 0,
              "item": {"type": "function_call", "call_id": "c_ours",
                       "name": wsl.GW_WEB_SEARCH_NAME, "arguments": '{"query":"q"}'}}),
        _raw({"type": "response.output_item.added", "output_index": 1,
              "item": {"type": "custom_tool_call", "call_id": "c_client", "name": "shell"}}),
        _raw({"type": "response.output_item.done", "output_index": 1,
              "item": {"type": "custom_tool_call", "call_id": "c_client",
                       "name": "shell", "input": "ls"}}),
        _raw({"type": "response.completed",
              "response": {"id": "resp_1", "status": "completed",
                           "output": [
                               {"type": "function_call", "call_id": "c_ours",
                                "name": wsl.GW_WEB_SEARCH_NAME},
                               {"type": "custom_tool_call", "call_id": "c_client",
                                "name": "shell", "input": "ls"},
                           ]}}),
    ]


async def test_a_custom_tool_call_in_the_same_turn_is_not_swallowed():
    """⚠️ 이 파일의 핵심. 클라이언트 도구 호출이 있으면 그 턴에서 **끝나야** 한다."""
    out, mcp, remaining = await _run_responses([_responses_parallel_turn()])
    evs = _events(out)

    assert "response.completed" in evs, f"종료하지 않았다: {evs}"
    assert mcp.calls == 0, (
        f"검색을 {mcp.calls}회 실행했다 — 클라이언트 도구 호출이 있는 턴은 terminal 이다"
    )
    assert len(remaining) == 0, "상류를 한 번만 불러야 한다"

    # 클라이언트 도구 호출이 실제로 전달됐는지
    added = _payloads(out, "response.output_item.added")
    kinds = [(a.get("item") or {}).get("type") for a in added]
    assert "custom_tool_call" in kinds, (
        f"클라이언트의 custom_tool_call 이 전달되지 않았다: {kinds} — 클라이언트는 자기가 "
        "실행해야 할 호출을 보지 못한 채 기다린다"
    )
    # 우리 것은 여전히 억제된다
    ours = [
        a for a in added
        if (a.get("item") or {}).get("name") == wsl.GW_WEB_SEARCH_NAME
    ]
    assert ours == [], f"우리 web_search 호출이 클라이언트에게 노출됐다: {ours}"


async def test_the_terminal_object_still_drops_our_call_but_keeps_the_clients():
    out, _mcp, _rem = await _run_responses([_responses_parallel_turn()])
    completed = _payloads(out, "response.completed")
    assert completed, "response.completed 가 없다"
    output = completed[-1]["response"]["output"]
    names = [o.get("name") for o in output]
    assert wsl.GW_WEB_SEARCH_NAME not in names, f"우리 배관이 최종 객체에 남았다: {names}"
    assert "shell" in names, f"클라이언트 도구 호출이 최종 객체에서 사라졌다: {names}"


# ─────────────────────────────────────────────────────────────────────────────
# #5 — 비스트리밍 본문에서 우리 배관이 제거된다
# ─────────────────────────────────────────────────────────────────────────────


async def _run_anthropic_nonstream(turns, *, max_iterations=3, **kw):
    queue = list(turns)
    mcp = kw.pop("mcp", None) or _Mcp()
    usages: list = []

    async def invoke(body):
        status, payload = queue.pop(0)
        return status, json.dumps(payload).encode(), {}, _usage()

    async def on_usage(u):
        usages.append(u)

    resp = await wsl._anthropic_nonstream(
        invoke=invoke,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=mcp,
        on_usage=on_usage,
        max_iterations=max_iterations,
        deadline=asyncio.get_event_loop().time() + 999,
        default_max_results=5,
        **kw,
    )
    return resp, usages, mcp


def _usage(inp: int = 10, out: int = 5):
    from app.schemas.domain import TokenUsage

    return TokenUsage(input_tokens=inp, output_tokens=out)


async def test_the_nonstream_body_does_not_leak_our_tool_use_block():
    """⚠️ 클라이언트는 선언하지도 않은 ``web_search`` 도구 호출을 받게 됐다."""
    body = {
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "let me look"},
            {"type": "tool_use", "id": "tu_ours", "name": wsl.GW_WEB_SEARCH_NAME, "input": {}},
            {"type": "tool_use", "id": "tu_client", "name": "Bash", "input": {"cmd": "ls"}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    resp, _usages, mcp = await _run_anthropic_nonstream([(200, body)])
    got = json.loads(bytes(resp.body))

    names = [b.get("name") for b in got["content"] if b.get("type") == "tool_use"]
    assert wsl.GW_WEB_SEARCH_NAME not in names, (
        f"우리 배관이 응답 본문에 남았다: {names} — 클라이언트는 없는 도구를 실행하려 하거나 "
        "다음 턴에서 400 을 받는다"
    )
    assert "Bash" in names, f"클라이언트 도구 호출이 사라졌다: {names}"
    assert got["stop_reason"] == "tool_use", (
        "클라이언트 도구 호출이 남아 있으므로 stop_reason 은 tool_use 여야 한다"
    )
    assert mcp.calls == 0, "클라이언트 도구가 있는 턴에서 검색을 돌렸다"


async def test_the_stop_reason_is_rewritten_when_only_our_call_was_present():
    """우리 것만 지웠는데 stop_reason 이 tool_use 로 남으면 클라이언트는 없는 호출을 기다린다.

    force_final 턴에서 실제로 일어난다 — 그 턴은 tools 없이 나가지만 모델이 여전히
    도구 호출을 시도할 수 있다.
    """
    body = {
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "tu_ours", "name": wsl.GW_WEB_SEARCH_NAME, "input": {}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    resp, _u, _m = await _run_anthropic_nonstream([(200, body)], max_iterations=0)
    got = json.loads(bytes(resp.body))
    assert got["content"] == [], f"우리 블록이 남았다: {got['content']}"
    assert got["stop_reason"] == "end_turn", (
        f"stop_reason 이 {got['stop_reason']} — 클라이언트가 볼 수 없는 도구 호출을 "
        "기다리게 된다"
    )


async def _run_responses_nonstream(turns, **kw):
    queue = list(turns)
    mcp = kw.pop("mcp", None) or _Mcp()
    usages: list = []

    async def invoke(body):
        status, payload = queue.pop(0)
        return status, json.dumps(payload).encode(), {}, _usage()

    async def on_usage(u):
        usages.append(u)

    resp = await wsl._responses_nonstream(
        invoke=invoke,
        base_body={"input": "hi"},
        mcp_client=mcp,
        on_usage=on_usage,
        max_iterations=3,
        deadline=asyncio.get_event_loop().time() + 999,
        default_max_results=5,
        **kw,
    )
    return resp, usages, mcp


async def test_the_responses_nonstream_body_does_not_leak_our_function_call():
    body = {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {"type": "function_call", "call_id": "c_ours", "name": wsl.GW_WEB_SEARCH_NAME},
            {"type": "custom_tool_call", "call_id": "c_client", "name": "shell", "input": "ls"},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    resp, _u, mcp = await _run_responses_nonstream([(200, body)])
    got = json.loads(bytes(resp.body))
    names = [o.get("name") for o in got["output"]]
    assert wsl.GW_WEB_SEARCH_NAME not in names, f"우리 배관이 남았다: {names}"
    assert "shell" in names, f"클라이언트 도구 호출이 사라졌다: {names}"
    assert mcp.calls == 0, "custom_tool_call 을 클라이언트 소유로 보지 않았다"


# ─────────────────────────────────────────────────────────────────────────────
# #1 — 첫 턴이 실패해도 예약 해제 콜백이 불린다
# ─────────────────────────────────────────────────────────────────────────────


async def test_on_usage_fires_even_when_the_first_turn_fails_with_no_tokens():
    """⚠️ 이 콜백이 ``finalize`` 이고, 그 zero-usage 경로가 예약을 되돌리는 유일한 지점이다.

    웹서치 분기는 폴백 루프를 타지 않으므로 ``release_reservations`` 도 돌지 않는다.
    """
    from app.schemas.domain import TokenUsage

    queue = [(400, {"error": {"type": "invalid_request_error", "message": "bad"}})]
    usages: list[TokenUsage] = []

    async def invoke(body):
        status, payload = queue.pop(0)
        return status, json.dumps(payload).encode(), {}, TokenUsage()

    async def on_usage(u):
        usages.append(u)

    resp = await wsl._anthropic_nonstream(
        invoke=invoke,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=_Mcp(),
        on_usage=on_usage,
        max_iterations=3,
        deadline=asyncio.get_event_loop().time() + 999,
        default_max_results=5,
    )
    assert resp.status_code == 400
    assert len(usages) == 1, (
        "토큰 0 인 실패에서 on_usage 가 불리지 않았다 — 예약이 창이 끝날 때까지 남는다"
    )
    assert usages[0].input_tokens == 0 and usages[0].output_tokens == 0


async def test_the_responses_loop_also_fires_on_a_zero_token_failure():
    from app.schemas.domain import TokenUsage

    queue = [(500, {"error": {"type": "api_error"}})]
    usages: list[TokenUsage] = []

    async def invoke(body):
        status, payload = queue.pop(0)
        return status, json.dumps(payload).encode(), {}, TokenUsage()

    async def on_usage(u):
        usages.append(u)

    resp = await wsl._responses_nonstream(
        invoke=invoke,
        base_body={"input": "hi"},
        mcp_client=_Mcp(),
        on_usage=on_usage,
        max_iterations=3,
        deadline=asyncio.get_event_loop().time() + 999,
        default_max_results=5,
    )
    assert resp.status_code == 500
    assert len(usages) == 1, "예약 해제 콜백이 불리지 않았다"


async def test_a_successful_turn_still_reports_its_real_usage():
    """⚠️ 대조군. 무조건 호출로 바꾼 것이 사용량을 0 으로 만들지 않는지."""
    body = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "answer"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    _resp, usages, _m = await _run_anthropic_nonstream([(200, body)])
    assert len(usages) == 1
    assert usages[0].input_tokens == 10, f"사용량이 {usages[0].input_tokens}"
    assert usages[0].output_tokens == 5


# ─────────────────────────────────────────────────────────────────────────────
# #4 — 봉투를 열지 않았으면 종료 프레임도 지어내지 않는다
# ─────────────────────────────────────────────────────────────────────────────


async def _run_anthropic(turns, **kw):
    queue = list(turns)
    mcp = kw.pop("mcp", None) or _Mcp()

    async def invoke_stream(body):
        frames = queue.pop(0)

        async def gen():
            for f in frames:
                yield f

        return 200, gen(), {}, None

    async def on_usage(_u):
        pass

    out: list[str] = []
    async for chunk in wsl._anthropic_stream(
        invoke_stream=invoke_stream,
        base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=mcp,
        request=None,
        on_usage=on_usage,
        max_iterations=3,
        deadline=asyncio.get_event_loop().time() + 999,
        default_max_results=5,
        **kw,
    ):
        out.append(chunk.decode())
    return out


async def test_no_message_stop_without_a_message_start():
    """⚠️ start 없는 stop 은 SDK 파싱 오류다 — 상류 오류가 게이트웨이 버그로 보인다.

    상류가 200 을 주면서 오류 이벤트만 보내는 경우다(어댑터가 실제로 그렇게 한다).
    """
    only_error = [
        _raw({"type": "error", "error": {"type": "provider_error", "message": "boom"}}),
    ]
    out = await _run_anthropic([only_error])
    evs = _events(out)
    assert "message_start" not in evs, "전제: 봉투가 열리지 않았다"
    assert "message_stop" not in evs, (
        f"봉투 없이 message_stop 을 보냈다: {evs} — Anthropic SSE 계약 위반이다"
    )
    assert "error" in evs, f"오류 프레임이 종료 신호여야 한다: {evs}"


async def test_no_message_delta_without_a_message_start():
    """종료 이벤트를 못 받은 경우에도 같다 — 지어낼 근거가 없다."""
    silent = [_raw({"type": "ping"})]
    out = await _run_anthropic([silent])
    evs = _events(out)
    assert "message_start" not in evs
    assert "message_delta" not in evs, f"봉투 없이 message_delta 를 보냈다: {evs}"
    assert "message_stop" not in evs, f"봉투 없이 message_stop 을 보냈다: {evs}"


async def test_the_envelope_is_still_closed_when_it_was_opened():
    """⚠️ 대조군. 봉투가 열렸으면 반드시 닫아야 한다 — 안 닫으면 클라이언트가 매달린다."""
    normal = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "hi"}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
              "usage": {"output_tokens": 3}}),
        _raw({"type": "message_stop"}),
    ]
    out = await _run_anthropic([normal])
    evs = _events(out)
    assert evs.count("message_start") == 1, evs
    assert evs[-1] == "message_stop", f"봉투를 닫지 않았다: {evs}"


async def test_an_opened_envelope_is_closed_even_when_the_turn_is_truncated():
    """열린 뒤 끊긴 경우 — 오류 프레임 + message_stop 으로 닫아야 한다."""
    truncated = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
    ]
    out = await _run_anthropic([truncated])
    evs = _events(out)
    assert "message_start" in evs
    assert "error" in evs, f"잘린 스트림을 성공으로 주장했다: {evs}"
    assert evs[-1] == "message_stop", f"열린 봉투를 닫지 않았다: {evs}"
    assert "message_delta" not in evs, "잘렸는데 종료 프레임을 지어냈다"


async def test_responses_emits_no_terminal_object_without_a_created():
    """Responses 도 같다 — created 없는 completed/failed 는 상관지을 대상이 없다."""
    only_error = [
        _raw({"type": "error", "error": {"type": "provider_error", "message": "boom"}}),
    ]
    out, _mcp, _rem = await _run_responses([only_error])
    evs = _events(out)
    assert "response.created" not in evs, "전제: 봉투가 열리지 않았다"
    for terminal in ("response.completed", "response.failed", "response.incomplete"):
        assert terminal not in evs, f"봉투 없이 {terminal} 을 보냈다: {evs}"
    assert "error" in evs, f"오류 프레임이 종료 신호여야 한다: {evs}"


async def test_responses_still_closes_an_opened_envelope():
    """⚠️ 대조군."""
    truncated = [
        _raw({"type": "response.created", "response": {"id": "resp_1"}}),
    ]
    out, _mcp, _rem = await _run_responses([truncated])
    evs = _events(out)
    assert "response.created" in evs
    assert "response.incomplete" in evs, f"열린 봉투를 닫지 않았다: {evs}"


async def test_an_anthropic_non_tool_use_call_shape_also_ends_the_turn():
    """⚠️ 헬퍼가 **호출 지점에서 실제로 쓰이는지**를 본다.

    헬퍼 단위 테스트만 있으면 스티처가 ``btype == "tool_use"`` 정확일치로 되돌아가도
    통과한다(대조군으로 확인했다 — 8개 중 이것만 터지지 않았다).

    ``mcp_tool_use`` 는 우리가 native 도구를 걷어내는 지금 구성에서는 나오지 않는다. 이
    테스트가 고정하는 것은 그 형태가 실재한다는 주장이 아니라, **모르는 도구 호출 형태를
    만났을 때 삼키지 않고 클라이언트에게 넘긴다**는 성질이다.
    """
    parallel = [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "tool_use", "id": "tu_ours",
                                "name": wsl.GW_WEB_SEARCH_NAME, "input": {}}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta", "partial_json": '{"query":"q"}'}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "content_block_start", "index": 1,
              "content_block": {"type": "mcp_tool_use", "id": "mt_1", "name": "remote_lookup"}}),
        _raw({"type": "content_block_stop", "index": 1}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
              "usage": {"output_tokens": 5}}),
        _raw({"type": "message_stop"}),
    ]
    mcp = _Mcp()
    out = await _run_anthropic([parallel], mcp=mcp)
    evs = _events(out)

    assert mcp.calls == 0, (
        f"검색을 {mcp.calls}회 돌렸다 — 알 수 없는 도구 호출 형태를 삼키고 루프를 돌렸다"
    )
    assert evs[-1] == "message_stop", f"봉투를 닫지 않았다: {evs}"

    starts = _payloads(out, "content_block_start")
    kinds = [(b.get("content_block") or {}).get("type") for b in starts]
    assert "mcp_tool_use" in kinds, (
        f"클라이언트 도구 호출이 전달되지 않았다: {kinds}"
    )
    # stop_reason 은 tool_use 로 남아야 한다 — 클라이언트가 실행할 호출이 실제로 있다.
    delta = _payloads(out, "message_delta")[-1]
    assert delta["delta"]["stop_reason"] == "tool_use", (
        f"클라이언트 도구 호출이 있는데 stop_reason 을 {delta['delta']['stop_reason']} 로 "
        "바꿨다 — 클라이언트는 결과를 돌려줄 이유를 잃는다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# #3 — 종료 프레임이 전 턴의 입력 합계를 말한다
# ─────────────────────────────────────────────────────────────────────────────


def _anthropic_search_turn(i: int, inp: int) -> list[bytes]:
    return [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": inp}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "tool_use", "id": f"tu_{i}",
                                "name": wsl.GW_WEB_SEARCH_NAME, "input": {}}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta", "partial_json": '{"query":"q"}'}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
              "usage": {"output_tokens": 5}}),
        _raw({"type": "message_stop"}),
    ]


def _anthropic_final(inp: int) -> list[bytes]:
    return [
        _raw({"type": "message_start", "message": {"usage": {"input_tokens": inp}}}),
        _raw({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}),
        _raw({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "answer"}}),
        _raw({"type": "content_block_stop", "index": 0}),
        _raw({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
              "usage": {"output_tokens": 3}}),
        _raw({"type": "message_stop"}),
    ]


async def test_the_terminal_frame_reports_the_input_summed_over_all_turns():
    """⚠️ ``message_start`` 는 첫 턴에 한 번만 나간다 — 그 값은 1턴치다.

    Claude Code 는 usage 로 컨텍스트를 추적하므로, N 턴을 돈 요청에서 그 값만 보면 우리가
    청구하는 양과 어긋난다.
    """
    out = await _run_anthropic([_anthropic_search_turn(1, 10), _anthropic_final(50)])
    starts = _payloads(out, "message_start")
    deltas = _payloads(out, "message_delta")
    assert len(starts) == 1, f"봉투가 {len(starts)}개 — 하나여야 한다"
    assert starts[0]["message"]["usage"]["input_tokens"] == 10, "전제: 첫 턴 값"

    assert deltas, "종료 프레임이 없다"
    final_usage = deltas[-1]["usage"]
    assert final_usage.get("input_tokens") == 60, (
        f"종료 프레임의 입력이 {final_usage.get('input_tokens')} — 10+50=60 이어야 한다"
    )
    assert final_usage.get("output_tokens") == 8, f"출력이 {final_usage.get('output_tokens')}"


async def test_the_terminal_frame_carries_the_cache_buckets_too():
    """input_tokens 만 고치면 details 와 서로 모순되는 payload 가 된다."""
    out = await _run_anthropic([_anthropic_final(50)])
    usage = _payloads(out, "message_delta")[-1]["usage"]
    for k in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens"):
        assert k in usage, f"{k} 가 없다: {usage}"
