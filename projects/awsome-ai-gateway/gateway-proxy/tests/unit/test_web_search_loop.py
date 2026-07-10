# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for the server-side web-search tool-use loop (Architecture C).

Covers the highest-risk behaviors:
- tool injection purity (client tools preserved; idempotent; strip on force-final)
- usage merge (reasoning NOT double-counted; total = in+out; booleans OR)
- Anthropic stitching: 0 searches (near-verbatim), 1 search (single envelope, search
  plumbing suppressed), client tool_use (terminal passthrough), search failure resilience
- Responses stitching: 1 search (single response.created/.completed), function_call suppressed

The loop takes bound invoke/invoke_stream callables (dialect-agnostic), so tests drive
it with fakes that emit adapter-shaped raw JSON event blobs / bodies.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from app.schemas.domain import TokenUsage
from app.services import web_search_loop as wsl
from app.services.agentcore_mcp_client import AgentCoreMcpError, WebSearchResponse


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


class FakeMcp:
    """Fake AgentCore MCP client. Records searches; can be told to fail."""

    def __init__(self, fail: bool = False, result_text: str = '{"results":[{"text":"hit","url":"http://x"}]}'):
        self.fail = fail
        self.result_text = result_text
        self.calls: list[tuple[str, int]] = []
        self.init_calls = 0

    async def ensure_initialized(self) -> str:
        self.init_calls += 1
        return "web-search-tool___WebSearch"

    async def search(self, query: str, max_results: int = 10) -> WebSearchResponse:
        self.calls.append((query, max_results))
        if self.fail:
            raise AgentCoreMcpError("boom")
        return WebSearchResponse(results=[], raw_text=self.result_text)


async def _aiter(items: list[bytes]) -> AsyncIterator[bytes]:
    for it in items:
        yield it


def _raw(obj: dict) -> bytes:
    return json.dumps(obj).encode()


def _parse_sse(out: bytes) -> list[tuple[str, dict]]:
    """Parse emitted SSE bytes into (event, data) tuples."""
    events = []
    for block in out.split(b"\n\n"):
        block = block.strip()
        if not block:
            continue
        ev = None
        data = None
        for line in block.split(b"\n"):
            if line.startswith(b"event:"):
                ev = line[len(b"event:"):].strip().decode()
            elif line.startswith(b"data:"):
                data = json.loads(line[len(b"data:"):].strip())
        events.append((ev, data))
    return events


# ── tool injection purity ───────────────────────────────────────────────────────
def test_injection_preserves_client_tools_and_is_idempotent():
    body = {"messages": [], "tools": [{"name": "client_a"}]}
    out1 = wsl._with_web_search_tool(body, "anthropic", include=True)
    names = [t["name"] for t in out1["tools"]]
    assert names == ["client_a", "web_search"]
    # idempotent: re-injecting doesn't duplicate
    out2 = wsl._with_web_search_tool(out1, "anthropic", include=True)
    assert [t["name"] for t in out2["tools"]] == ["client_a", "web_search"]
    # original body untouched (shallow copy semantics for tools)
    assert [t["name"] for t in body["tools"]] == ["client_a"]


def test_injection_force_final_strips_our_tool_but_keeps_client():
    body = {"messages": [], "tools": [{"name": "client_a"}, wsl._anthropic_tool_def()]}
    out = wsl._with_web_search_tool(body, "anthropic", include=False)
    assert [t["name"] for t in out["tools"]] == ["client_a"]


def test_injection_responses_shape():
    out = wsl._with_web_search_tool({"input": "hi"}, "responses", include=True)
    tool = out["tools"][-1]
    assert tool["type"] == "function" and tool["name"] == "web_search"


def test_injection_no_tools_key_when_empty_and_excluded():
    out = wsl._with_web_search_tool({"input": "hi"}, "responses", include=False)
    assert "tools" not in out


# ── usage merge ──────────────────────────────────────────────────────────────────
def test_merge_usage_reasoning_not_double_counted():
    acc = TokenUsage()
    wsl._merge_usage(acc, TokenUsage(input_tokens=10, output_tokens=20, reasoning_tokens=8))
    wsl._merge_usage(acc, TokenUsage(input_tokens=5, output_tokens=7, reasoning_tokens=3))
    assert acc.input_tokens == 15
    assert acc.output_tokens == 27
    assert acc.total_tokens == 42  # in+out, reasoning NOT added
    assert acc.reasoning_tokens == 11  # summed for visibility only


def test_merge_usage_booleans_or():
    acc = TokenUsage()
    wsl._merge_usage(acc, TokenUsage(cache_ttl_1h=True))
    wsl._merge_usage(acc, TokenUsage(estimated=True))
    assert acc.cache_ttl_1h is True and acc.estimated is True


# ── Anthropic stitching ────────────────────────────────────────────────────────────
def _anthropic_turn(text: str, *, usage_in=10, usage_out=5, tool_use=None) -> list[bytes]:
    """Build one Anthropic streaming turn's raw event blobs.

    tool_use: optional {"id","name","input"} → emits a tool_use content block + stop_reason.
    """
    events = [
        {"type": "message_start", "message": {"id": "msg_x", "type": "message", "role": "assistant",
         "content": [], "usage": {"input_tokens": usage_in, "output_tokens": 0}}},
    ]
    idx = 0
    if text:
        events += [
            {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": idx},
        ]
        idx += 1
    stop = "end_turn"
    if tool_use:
        stop = "tool_use"
        events += [
            {"type": "content_block_start", "index": idx,
             "content_block": {"type": "tool_use", "id": tool_use["id"], "name": tool_use["name"], "input": {}}},
            {"type": "content_block_delta", "index": idx,
             "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_use["input"])}},
            {"type": "content_block_stop", "index": idx},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": usage_out}},
        {"type": "message_stop"},
    ]
    return [_raw(e) for e in events]


async def _run_anthropic_stream(turns: list[list[bytes]], mcp, **kw):
    """Drive _anthropic_stream with a fake invoke_stream that returns queued turns."""
    seq = iter(turns)
    captured = {}

    async def on_usage(u):
        captured["usage"] = u

    async def invoke_stream(_body):
        return 200, _aiter(next(seq)), {}, None

    import time
    out = b""
    gen = wsl._anthropic_stream(
        invoke_stream=invoke_stream, base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=mcp, request=FakeRequest(), on_usage=on_usage,
        max_iterations=5, deadline=time.monotonic() + 60, default_max_results=10,
    )
    async for frame in gen:
        out += frame
    return out, captured.get("usage")


@pytest.mark.asyncio
async def test_anthropic_zero_search_single_envelope():
    turns = [_anthropic_turn("Hello world")]
    out, usage = await _run_anthropic_stream(turns, FakeMcp())
    events = _parse_sse(out)
    types = [e for e, _ in events]
    assert types.count("message_start") == 1
    assert types.count("message_stop") == 1
    # text delta forwarded
    assert any(e == "content_block_delta" and d.get("delta", {}).get("text") == "Hello world"
               for e, d in events)
    assert usage.web_search_count == 0


@pytest.mark.asyncio
async def test_anthropic_one_search_single_envelope_suppresses_plumbing():
    turns = [
        _anthropic_turn("Let me search", tool_use={"id": "toolu_1", "name": "web_search",
                                                    "input": {"query": "aws news"}}),
        _anthropic_turn("Final answer with cite"),
    ]
    mcp = FakeMcp()
    out, usage = await _run_anthropic_stream(turns, mcp)
    events = _parse_sse(out)
    types = [e for e, _ in events]
    # EXACTLY one envelope despite two model turns
    assert types.count("message_start") == 1, types
    assert types.count("message_stop") == 1, types
    # search actually ran once
    assert mcp.calls == [("aws news", 10)]
    assert usage.web_search_count == 1
    # web_search tool_use block NOT surfaced as a tool_use content block to the client
    assert not any(
        e == "content_block_start" and d.get("content_block", {}).get("type") == "tool_use"
        for e, d in events
    )
    # both turns' text forwarded
    texts = [d["delta"]["text"] for e, d in events
             if e == "content_block_delta" and d.get("delta", {}).get("type") == "text_delta"]
    assert "Let me search" in texts and "Final answer with cite" in texts
    # indices monotonic within the single envelope
    starts = [d["index"] for e, d in events if e == "content_block_start"]
    assert starts == sorted(starts) and len(starts) == len(set(starts))


@pytest.mark.asyncio
async def test_anthropic_client_tool_is_terminal_passthrough():
    # A client (non-web_search) tool_use must terminate the loop and pass through.
    turns = [
        _anthropic_turn("", tool_use={"id": "toolu_c", "name": "read_file", "input": {"path": "/x"}}),
    ]
    mcp = FakeMcp()
    out, usage = await _run_anthropic_stream(turns, mcp)
    events = _parse_sse(out)
    # client tool_use IS surfaced
    assert any(e == "content_block_start" and d.get("content_block", {}).get("name") == "read_file"
               for e, d in events)
    # no search performed
    assert mcp.calls == []
    assert usage.web_search_count == 0


@pytest.mark.asyncio
async def test_anthropic_search_failure_still_completes():
    turns = [
        _anthropic_turn("searching", tool_use={"id": "toolu_1", "name": "web_search",
                                               "input": {"query": "q"}}),
        _anthropic_turn("answer from memory"),
    ]
    mcp = FakeMcp(fail=True)
    out, usage = await _run_anthropic_stream(turns, mcp)
    events = _parse_sse(out)
    types = [e for e, _ in events]
    assert types.count("message_stop") == 1
    # failed search NOT counted as successful
    assert usage.web_search_count == 0
    # loop still fed a tool_result and got the final turn (2 turns consumed)
    assert any(e == "content_block_delta" and d.get("delta", {}).get("text") == "answer from memory"
               for e, d in events)


# ── Responses stitching ────────────────────────────────────────────────────────────
def _responses_turn(text: str, *, fn_call=None, usage_in=12, usage_out=6, reasoning=4) -> list[bytes]:
    events = [{"type": "response.created", "response": {"id": "resp_x"}}]
    oidx = 0
    if text:
        events += [
            {"type": "response.output_item.added", "output_index": oidx,
             "item": {"type": "message", "role": "assistant"}},
            {"type": "response.output_text.delta", "output_index": oidx, "delta": text},
            {"type": "response.output_item.done", "output_index": oidx,
             "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}},
        ]
        oidx += 1
    if fn_call:
        events += [
            {"type": "response.output_item.added", "output_index": oidx,
             "item": {"type": "function_call", "name": fn_call["name"], "call_id": fn_call["call_id"]}},
            {"type": "response.function_call_arguments.delta", "output_index": oidx,
             "delta": json.dumps(fn_call["input"])},
            {"type": "response.function_call_arguments.done", "output_index": oidx},
            {"type": "response.output_item.done", "output_index": oidx,
             "item": {"type": "function_call", "name": fn_call["name"], "call_id": fn_call["call_id"],
                      "arguments": json.dumps(fn_call["input"])}},
        ]
    events.append({"type": "response.completed", "response": {"id": "resp_x", "usage": {
        "input_tokens": usage_in, "output_tokens": usage_out, "total_tokens": usage_in + usage_out,
        "output_tokens_details": {"reasoning_tokens": reasoning}}}})
    return [_raw(e) for e in events]


async def _run_responses_stream(turns, mcp):
    seq = iter(turns)
    captured = {}

    async def on_usage(u):
        captured["usage"] = u

    async def invoke_stream(_body):
        return 200, _aiter(next(seq)), {}, None

    import time
    out = b""
    gen = wsl._responses_stream(
        invoke_stream=invoke_stream, base_body={"input": "hi"},
        mcp_client=mcp, request=FakeRequest(), on_usage=on_usage,
        max_iterations=5, deadline=time.monotonic() + 60, default_max_results=10,
    )
    async for frame in gen:
        out += frame
    return out, captured.get("usage")


@pytest.mark.asyncio
async def test_responses_one_search_single_envelope():
    turns = [
        _responses_turn("", fn_call={"name": "web_search", "call_id": "call_1", "input": {"query": "aws"}}),
        _responses_turn("final answer"),
    ]
    mcp = FakeMcp()
    out, usage = await _run_responses_stream(turns, mcp)
    events = _parse_sse(out)
    types = [e for e, _ in events]
    assert types.count("response.created") == 1, types
    assert types.count("response.completed") == 1, types
    assert mcp.calls == [("aws", 10)]
    assert usage.web_search_count == 1
    # our function_call plumbing suppressed (no function_call_arguments deltas surfaced)
    assert not any(e == "response.function_call_arguments.delta" for e, _ in events)
    # final text forwarded
    assert any(e == "response.output_text.delta" and d.get("delta") == "final answer"
               for e, d in events)
    # merged usage across 2 turns
    assert usage.input_tokens == 24 and usage.output_tokens == 12 and usage.total_tokens == 36
    assert usage.reasoning_tokens == 8


@pytest.mark.asyncio
async def test_responses_zero_search_single_envelope():
    turns = [_responses_turn("just an answer")]
    mcp = FakeMcp()
    out, usage = await _run_responses_stream(turns, mcp)
    types = [e for e, _ in _parse_sse(out)]
    assert types.count("response.created") == 1
    assert types.count("response.completed") == 1
    assert usage.web_search_count == 0


# ── Codex cross-verification regression tests (F-1,F-3,F-5,F-7) ──────────────────
def _anthropic_turn_multi_search(searches: list[dict]) -> list[bytes]:
    """One Anthropic turn emitting MANY web_search tool_use blocks (F-3)."""
    events = [{"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
               "content": [], "usage": {"input_tokens": 10, "output_tokens": 0}}}]
    for i, s in enumerate(searches):
        events += [
            {"type": "content_block_start", "index": i,
             "content_block": {"type": "tool_use", "id": s["id"], "name": "web_search", "input": {}}},
            {"type": "content_block_delta", "index": i,
             "delta": {"type": "input_json_delta", "partial_json": json.dumps(s["input"])}},
            {"type": "content_block_stop", "index": i},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    return [_raw(e) for e in events]


@pytest.mark.asyncio
async def test_anthropic_multiple_searches_one_turn(monkeypatch):
    """F-3: two web_search calls in ONE turn → two tool_results, matched ids, both run."""
    turns = [
        _anthropic_turn_multi_search([
            {"id": "toolu_a", "input": {"query": "q1"}},
            {"id": "toolu_b", "input": {"query": "q2"}},
        ]),
        _anthropic_turn("final"),
    ]
    mcp = FakeMcp()
    # capture the conversation the loop builds for the 2nd turn by intercepting invoke_stream
    bodies = []
    seq = iter(turns)

    async def on_usage(u):
        pass

    async def invoke_stream(body):
        bodies.append(body)
        return 200, _aiter(next(seq)), {}, None

    import time as _t
    out = b""
    async for f in wsl._anthropic_stream(
        invoke_stream=invoke_stream, base_body={"messages": [{"role": "user", "content": "hi"}]},
        mcp_client=mcp, request=FakeRequest(), on_usage=on_usage,
        max_iterations=5, deadline=_t.monotonic() + 60, default_max_results=10,
    ):
        out += f
    # both searches executed
    assert mcp.calls == [("q1", 10), ("q2", 10)]
    # 2nd turn body's conversation has a user turn with TWO tool_results, matching ids
    second = bodies[1]
    user_turns = [m for m in second["messages"] if m["role"] == "user"]
    tool_result_msg = user_turns[-1]["content"]
    ids = [b["tool_use_id"] for b in tool_result_msg if b.get("type") == "tool_result"]
    assert ids == ["toolu_a", "toolu_b"], ids  # one result per tool_use, in order


@pytest.mark.asyncio
async def test_client_owns_web_search_tool_skips_loop():
    """F-7: client declares its own web_search tool → loop skipped, no hijack, MCP untouched."""
    mcp = FakeMcp()
    captured = {}

    async def on_usage(u):
        captured["usage"] = u

    async def invoke(body):
        # tool must still be present (we didn't strip it)
        captured["body"] = body
        return 200, json.dumps({"content": [{"type": "text", "text": "ok"}],
                                "stop_reason": "end_turn",
                                "usage": {"input_tokens": 5, "output_tokens": 2}}).encode(), {}, TokenUsage(input_tokens=5, output_tokens=2)

    async def invoke_stream(body):
        raise AssertionError("should not stream")

    resp = await wsl.run_web_search_loop(
        dialect="anthropic", invoke=invoke, invoke_stream=invoke_stream,
        initial_req_data={"messages": [{"role": "user", "content": "hi"}],
                          "tools": [{"name": "web_search", "description": "client's own"}]},
        is_stream=False, mcp_client=mcp, request=FakeRequest(), on_usage=on_usage,
    )
    assert mcp.init_calls == 0  # never initialized → loop skipped
    assert mcp.calls == []
    # client's web_search tool preserved (not hijacked)
    assert any(t.get("name") == "web_search" for t in captured["body"]["tools"])


@pytest.mark.asyncio
async def test_responses_incomplete_not_reported_as_completed():
    """F-1: upstream response.incomplete must NOT be turned into response.completed."""
    turn = [_raw({"type": "response.created", "response": {"id": "r"}}),
            _raw({"type": "response.output_text.delta", "output_index": 0, "delta": "partial"}),
            _raw({"type": "response.incomplete",
                  "response": {"id": "r", "status": "incomplete",
                               "usage": {"input_tokens": 5, "output_tokens": 3}}})]
    mcp = FakeMcp()
    out, usage = await _run_responses_stream([turn], mcp)
    types = [e for e, _ in _parse_sse(out)]
    assert "response.incomplete" in types
    assert "response.completed" not in types  # must not fake completion


@pytest.mark.asyncio
async def test_responses_final_output_strips_our_function_call():
    """F-3 Responses (round2): terminal response.output must NOT contain our web_search
    function_call (would be a dangling reference the client never saw)."""
    # Turn 1: search. The completed event's response.output includes the web_search function_call
    # (as upstream would); we must strip it. Then a 2nd turn with the final answer.
    turn1 = [
        _raw({"type": "response.created", "response": {"id": "r"}}),
        _raw({"type": "response.output_item.added", "output_index": 0,
              "item": {"type": "function_call", "name": "web_search", "call_id": "call_x"}}),
        _raw({"type": "response.function_call_arguments.delta", "output_index": 0,
              "delta": json.dumps({"query": "q"})}),
        _raw({"type": "response.function_call_arguments.done", "output_index": 0}),
        _raw({"type": "response.output_item.done", "output_index": 0,
              "item": {"type": "function_call", "name": "web_search", "call_id": "call_x",
                       "arguments": json.dumps({"query": "q"})}}),
        _raw({"type": "response.completed",
              "response": {"id": "r", "status": "completed",
                           "output": [{"type": "function_call", "name": "web_search", "call_id": "call_x"}],
                           "usage": {"input_tokens": 8, "output_tokens": 4}}}),
    ]
    turn2 = _responses_turn("final answer")
    mcp = FakeMcp()
    out, usage = await _run_responses_stream([turn1, turn2], mcp)
    events = _parse_sse(out)
    completed = [d for e, d in events if e == "response.completed"]
    assert completed, "should emit one response.completed"
    final_output = completed[-1]["response"].get("output", [])
    # our web_search call_id must be stripped
    assert not any(it.get("type") == "function_call" and it.get("call_id") == "call_x"
                   for it in final_output), final_output


@pytest.mark.asyncio
async def test_responses_error_not_followed_by_fake_completed():
    """round2 High-1: a mid-stream error must NOT be followed by a synthetic response.completed."""
    turn = [
        _raw({"type": "response.created", "response": {"id": "r"}}),
        _raw({"type": "response.output_text.delta", "output_index": 0, "delta": "part"}),
        _raw({"type": "error", "error": {"type": "server_error", "message": "boom"}}),
    ]
    mcp = FakeMcp()
    out, usage = await _run_responses_stream([turn], mcp)
    types = [e for e, _ in _parse_sse(out)]
    assert "error" in types
    assert "response.completed" not in types  # no fake success after error
    assert "response.failed" in types  # closed cleanly with failed
