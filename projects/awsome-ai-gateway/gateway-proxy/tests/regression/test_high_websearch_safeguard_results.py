"""웹 검색 루프도 서버 분류기 판정(safeguard_results)을 그대로 전달한다 (2026-10-06).

웹 검색이 켜진 앱에서는 게이트웨이가 Bedrock 을 여러 번(턴) 부르고, 클라이언트에게는
응답 한 벌로 이어 붙여 보낸다. 스트리밍은 마지막 ``message_delta`` 를 게이트웨이가
보내는데, 예전에는 이것을 새로 만들면서 ``delta.safeguard_results`` 를 버렸다. 그러면
Claude Code 는 판정이 없다고 보고(server_no_result) 세션 내내 로컬 분류기로 바꾼다.

계약:
- 마지막 턴의 delta 를 복사하고 ``stop_reason`` 만 게이트웨이 값으로 덮어쓴다;
- 판정은 마지막 턴 것만 나간다(앞선 검색 턴 판정은 우리 검색 호출에 대한 것) —
  Claude Code 는 판정 항목이 정확히 하나여야 인식한다;
- ``tool_use`` → ``end_turn`` 재작성에도 판정은 유지한다;
- 비스트리밍은 마지막 턴 응답의 최상위 ``safeguard_results`` 를 그대로 둔다.

English: the web-search loop passes the server-side classifier verdicts through
(2026-10-06). The streaming stitcher used to rebuild the terminal
``message_delta`` and drop ``delta.safeguard_results``, so Claude Code saw no verdict
(server_no_result) and classified locally for the whole session. Contract: copy the
last turn's delta and override only ``stop_reason``; only the last turn's verdicts go
out (earlier search turns' verdicts cover our own search calls) — Claude Code accepts
exactly one entry; verdicts survive the ``tool_use`` → ``end_turn`` rewrite; the
non-streaming path keeps the last turn's top-level ``safeguard_results``.
"""

from __future__ import annotations

import json
import time

from app.schemas.domain import TokenUsage
from app.services import web_search_loop as wsl
from tests.unit.test_web_search_loop import FakeMcp, FakeRequest, _aiter, _parse_sse

BASH = {"name": "Bash", "description": "run", "input_schema": {"type": "object"}}
SEARCH = ("toolu_S1", "web_search", {"query": "news"})
CLIENT = ("toolu_B", "Bash", {"command": "ls"})


def _verdicts(*ids: str) -> list[dict]:
    return [{"type": "dangerous_tool_use",
             "status": {"type": "available", "tool_uses": {
                 i: {"type": "evaluated", "outcome": "not_flagged"} for i in ids}}}]


def _turn(*, text: str = "", tool=None, stop=None, delta_extra=None) -> list[bytes]:
    """Bedrock 스트리밍 한 턴. English: one Bedrock streaming turn."""
    ev = [{"type": "message_start", "message": {
        "id": "msg_x", "type": "message", "role": "assistant", "content": [],
        "usage": {"input_tokens": 10, "output_tokens": 0}}}]
    idx = 0
    if text:
        ev += [{"type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""}},
               {"type": "content_block_delta", "index": idx,
                "delta": {"type": "text_delta", "text": text}},
               {"type": "content_block_stop", "index": idx}]
        idx += 1
    if tool:
        tid, name, args = tool
        ev += [{"type": "content_block_start", "index": idx, "content_block": {
                    "type": "tool_use", "id": tid, "name": name, "input": {}}},
               {"type": "content_block_delta", "index": idx, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(args)}},
               {"type": "content_block_stop", "index": idx}]
    delta = {"stop_reason": stop or ("tool_use" if tool else "end_turn"),
             **(delta_extra or {})}
    ev += [{"type": "message_delta", "delta": delta, "usage": {"output_tokens": 5}},
           {"type": "message_stop"}]
    return [json.dumps(e).encode() for e in ev]


async def _deltas(turns: list[list[bytes]]) -> list[dict]:
    """턴들을 흘려 보내고 클라이언트가 받은 message_delta 들을 돌려준다.

    English: stream the turns and return the message_delta events the client got.
    """
    seq = iter(turns)

    async def invoke_stream(_body):
        return 200, _aiter(next(seq)), {}, None

    async def on_usage(_u):
        return None

    out = b""
    async for frame in wsl._anthropic_stream(
            invoke_stream=invoke_stream,
            base_body={"messages": [{"role": "user", "content": "hi"}],
                       "tools": [BASH]},
            mcp_client=FakeMcp(), request=FakeRequest(), on_usage=on_usage,
            max_iterations=5, deadline=time.monotonic() + 60,
            default_max_results=10):
        out += frame
    return [d for e, d in _parse_sse(out) if e == "message_delta"]


# ── 스트리밍 / streaming ─────────────────────────────────────────────────────
async def test_single_turn_client_tool_keeps_the_verdicts():
    turn = _turn(text="ok", tool=CLIENT,
                 delta_extra={"safeguard_results": _verdicts("toolu_B")})
    [md] = await _deltas([turn])
    assert md["delta"]["safeguard_results"] == _verdicts("toolu_B")
    assert md["delta"]["stop_reason"] == "tool_use"


async def test_after_a_search_only_the_last_turns_verdicts_go_out():
    search = _turn(tool=SEARCH,
                   delta_extra={"safeguard_results": _verdicts("toolu_S1")})
    last = _turn(text="done", tool=CLIENT,
                 delta_extra={"safeguard_results": _verdicts("toolu_B")})
    deltas = await _deltas([search, last])
    assert len(deltas) == 1                          # one envelope, one end
    results = deltas[0]["delta"]["safeguard_results"]
    assert len(results) == 1                         # exactly one entry
    assert set(results[0]["status"]["tool_uses"]) == {"toolu_B"}


async def test_search_turn_verdicts_are_not_carried_forward():
    search = _turn(tool=SEARCH,
                   delta_extra={"safeguard_results": _verdicts("toolu_S1")})
    last = _turn(text="done")                        # no verdicts this turn
    [md] = await _deltas([search, last])
    assert "safeguard_results" not in md["delta"]


async def test_end_turn_rewrite_keeps_the_verdicts():
    # 클라이언트가 볼 수 없는 tool_use 로 끝나면 end_turn 으로 바뀐다.
    # English: a tool_use stop without a client tool becomes end_turn.
    last = _turn(text="done", stop="tool_use",
                 delta_extra={"safeguard_results": _verdicts()})
    [md] = await _deltas([last])
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["delta"]["safeguard_results"] == _verdicts()


async def test_other_delta_fields_pass_through():
    extra = {"stop_sequence": "###", "stop_details": None,
             "container": {"id": "c1"}, "future_field": 1}
    [md] = await _deltas([_turn(text="done", stop="stop_sequence",
                                delta_extra=extra)])
    assert md["delta"] == {"stop_reason": "stop_sequence", **extra}


# ── 비스트리밍 / non-streaming ───────────────────────────────────────────────
async def test_nonstream_keeps_the_last_turns_verdicts():
    def body(tool, ids):
        tid, name, args = tool
        return {"id": "m", "type": "message", "role": "assistant",
                "content": [{"type": "tool_use", "id": tid, "name": name,
                             "input": args}],
                "stop_reason": "tool_use", "safeguard_results": _verdicts(*ids),
                "usage": {"input_tokens": 10, "output_tokens": 5}}

    queue = [body(SEARCH, ["toolu_S1"]), body(CLIENT, ["toolu_B"])]

    async def invoke(_body):
        return (200, json.dumps(queue.pop(0)).encode(), {},
                TokenUsage(input_tokens=10, output_tokens=5))

    async def on_usage(_u):
        return None

    resp = await wsl._anthropic_nonstream(
        invoke=invoke,
        base_body={"messages": [{"role": "user", "content": "hi"}], "tools": [BASH]},
        mcp_client=FakeMcp(), on_usage=on_usage, max_iterations=5,
        deadline=time.monotonic() + 60, default_max_results=10)
    assert json.loads(resp.body)["safeguard_results"] == _verdicts("toolu_B")
