# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Server-side web-search tool-use loop (Architecture C) — 1P-style server search.

Bedrock/Mantle do NOT expose Anthropic's native server-side web_search (verified:
ValidationException). So this gateway emulates it: inject a `web_search` tool, run
the model, intercept OUR tool_use, call AgentCore Gateway's managed WebSearch over
MCP, feed the result back, and continue — stitching every model turn into a SINGLE
continuous client stream while suppressing the internal search plumbing. The client
declares nothing and sees one uninterrupted answer, exactly like 1P Claude search.

Design:
- ALWAYS stream every turn (is_stream=True path). We parse events as they arrive and
  buffer only the web_search tool_use blocks. This preserves token-by-token streaming
  even when no search happens (the stitcher degrades to near-verbatim re-emission),
  so there is no separate "fast path" to keep correct.
- Non-streaming client requests loop with invoke() and return the final assembled body.
- Backend-agnostic: the router passes bound `invoke`/`invoke_stream` callables (Bedrock
  uses path_suffix kwargs; Mantle uses profile/endpoint), so this module never touches
  adapter-specific kwargs.

Interception rule (both dialects):
- A turn with OUR web_search tool_use (and no client tool) → run search, continue.
- A turn with a CLIENT tool_use (any non-web_search) → TERMINAL, forward verbatim so the
  client's own tool loop runs. Never partially strip a multi-tool assistant message.
- A text-only / non-tool-stop turn → TERMINAL (final answer).
- Guardrails: max_iterations, total deadline. On hit, the next turn drops the web_search
  tool so the model must answer terminally. Search failures inject an error tool_result
  (stream never dies). web_search_count counts only SUCCESSFUL searches.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Optional

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.schemas.domain import TokenUsage
from app.services.agentcore_mcp_client import AgentCoreMcpClient, AgentCoreMcpError

logger = structlog.get_logger(__name__)

# Model-facing tool name we inject and match on to intercept. Decoupled from the MCP
# tool name (<target>___WebSearch) so interception is a simple name equality check.
GW_WEB_SEARCH_NAME = "web_search"

_WEB_SEARCH_DESCRIPTION = (
    "Search the public web for current, factual, or recent information. Use this when "
    "the answer may depend on events, data, docs, or facts that are recent or external. "
    "Returns titles, URLs, and snippets to cite."
)

# The loop passes the LOGICAL turn body (a dict: messages/input + tools + stream flag).
# The router's bound callable applies the adapter-specific PHYSICAL transform
# (_BEDROCK_ALLOWED_FIELDS filter, anthropic_version / model id, metadata) and invokes
# the adapter. This keeps body-shaping ownership in the router, backend-agnostic here.
InvokeFn = Callable[[dict], Awaitable[tuple[int, bytes, dict, TokenUsage]]]
InvokeStreamFn = Callable[[dict], Awaitable[tuple[int, AsyncIterator[bytes], dict, Optional[str]]]]


# ── tool injection (pure) ─────────────────────────────────────────────────────
def _anthropic_tool_def() -> dict:
    return {
        "name": GW_WEB_SEARCH_NAME,
        "description": _WEB_SEARCH_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (<=200 chars)"},
                "max_results": {
                    "type": "integer",
                    "description": "Max results (1-25)",
                    "minimum": 1,
                    "maximum": 25,
                },
            },
            "required": ["query"],
        },
    }


def _responses_tool_def() -> dict:
    return {
        "type": "function",
        "name": GW_WEB_SEARCH_NAME,
        "description": _WEB_SEARCH_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (<=200 chars)"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }


def _with_web_search_tool(body: dict, dialect: str, include: bool) -> dict:
    """Return a shallow copy of body with the web_search tool appended (or removed).

    Preserves any client-provided tools. ``include=False`` strips our tool (used for the
    forced-final turn after a guardrail) so the model cannot search again and must answer.
    """
    out = dict(body)
    existing = list(out.get("tools") or [])
    # Drop any prior copy of our tool (idempotent across turns).
    existing = [t for t in existing if not _is_our_tool(t)]
    if include:
        existing.append(_anthropic_tool_def() if dialect == "anthropic" else _responses_tool_def())
    if existing:
        out["tools"] = existing
    elif "tools" in out:
        out.pop("tools")
    return out


def _is_our_tool(tool: dict) -> bool:
    return isinstance(tool, dict) and tool.get("name") == GW_WEB_SEARCH_NAME


def _client_declares_web_search(body: dict) -> bool:
    """True if the client's ORIGINAL request already declares a tool named web_search.

    If so we must NOT inject/hijack it (F-7) — the loop is skipped and the request passes
    through so the client's own tool loop runs unmodified.
    """
    for t in (body.get("tools") or []):
        if isinstance(t, dict) and t.get("name") == GW_WEB_SEARCH_NAME:
            return True
    return False


# ── usage merge ───────────────────────────────────────────────────────────────
def _merge_usage(acc: TokenUsage, turn: TokenUsage) -> TokenUsage:
    """Sum usage across turns. reasoning_tokens stays a submetric (already inside
    output_tokens) — summed for visibility but total is recomputed from input+output,
    never with reasoning re-added. Booleans OR."""
    acc.input_tokens += turn.input_tokens
    acc.output_tokens += turn.output_tokens
    acc.cache_creation_input_tokens += turn.cache_creation_input_tokens
    acc.cache_read_input_tokens += turn.cache_read_input_tokens
    acc.reasoning_tokens += turn.reasoning_tokens
    acc.total_tokens = acc.input_tokens + acc.output_tokens
    acc.cache_ttl_1h = acc.cache_ttl_1h or turn.cache_ttl_1h
    acc.estimated = acc.estimated or turn.estimated
    return acc


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


# ── search execution (shared) ─────────────────────────────────────────────────
async def _do_search(
    mcp_client: AgentCoreMcpClient, tool_input: dict, default_max: int
) -> tuple[str, bool]:
    """Run one web search. Returns (result_text_for_model, ok). Never raises — on
    failure returns an error string so the model can continue from its own knowledge."""
    query = ""
    max_results = default_max
    if isinstance(tool_input, dict):
        query = str(tool_input.get("query") or "")
        try:
            max_results = int(tool_input.get("max_results") or default_max)
        except (TypeError, ValueError):
            max_results = default_max
    try:
        resp = await mcp_client.search(query, max_results)
        return resp.raw_text, True
    except AgentCoreMcpError as e:
        logger.warning("web_search.failed", error=str(e)[:200])
        return json.dumps({"error": f"web search unavailable: {str(e)[:160]}"}), False
    except Exception as e:  # defensive — never kill the stream
        logger.exception("web_search.unexpected")
        return json.dumps({"error": f"web search error: {str(e)[:160]}"}), False


# ════════════════════════════════════════════════════════════════════════════════
# ANTHROPIC (Messages) — streaming stitcher
# ════════════════════════════════════════════════════════════════════════════════
async def _anthropic_stream(
    *,
    invoke_stream: InvokeStreamFn,
    base_body: dict,
    mcp_client: AgentCoreMcpClient,
    request: Request,
    on_usage: Callable[[TokenUsage], Awaitable[None]],
    max_iterations: int,
    deadline: float,
    default_max_results: int,
) -> AsyncIterator[bytes]:
    """Stitch N Anthropic model turns into ONE message_start … message_stop stream.

    Forwards text/thinking blocks (re-indexed into one envelope); suppresses web_search
    tool_use/tool_result plumbing; runs the search between turns.
    """
    merged = TokenUsage()
    conversation: list[dict] = list(base_body.get("messages") or [])
    searches_done = 0        # successful searches → web_search_count (billing/attribution)
    search_attempts = 0      # ALL search rounds incl. failures → loop guard (F-5)
    envelope_open = False
    global_index = 0  # next content_block index in the stitched envelope
    stop_reason_final = "end_turn"

    try:
        while True:
            force_final = search_attempts >= max_iterations or time.monotonic() > deadline
            turn_body = _with_web_search_tool(base_body, "anthropic", include=not force_final)
            turn_body = dict(turn_body)
            turn_body["messages"] = conversation
            turn_body["stream"] = True

            status, chunk_iter, _headers, _rid = await invoke_stream(turn_body)
            if status != 200:
                async for b in _drain_error(chunk_iter, envelope_open):
                    yield b
                return

            # Per-turn parse state.
            assistant_content: list[dict] = []
            local_to_global: dict[int, int] = {}   # local block idx → emitted global idx
            suppressed: dict[int, dict] = {}        # local idx → {kind, buffer, block}
            text_buf: dict[int, str] = {}
            thinking_buf: dict[int, dict] = {}
            pending_searches: list[dict] = []   # [{id, name, input}] — support MANY per turn (F-3)
            client_tool_present = False

            async for raw in chunk_iter:
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = ev.get("type")

                if etype == "message_start":
                    u = (ev.get("message") or {}).get("usage") or {}
                    merged.input_tokens += int(u.get("input_tokens", 0) or 0)
                    merged.cache_creation_input_tokens += int(u.get("cache_creation_input_tokens", 0) or 0)
                    merged.cache_read_input_tokens += int(u.get("cache_read_input_tokens", 0) or 0)
                    if not envelope_open:
                        envelope_open = True
                        yield _sse("message_start", ev)

                elif etype == "content_block_start":
                    idx = ev.get("index", 0)
                    block = ev.get("content_block") or {}
                    btype = block.get("type")
                    if btype == "tool_use" and block.get("name") == GW_WEB_SEARCH_NAME:
                        # OUR search — suppress, buffer input JSON.
                        suppressed[idx] = {"kind": "web_search", "buf": "",
                                           "id": block.get("id"), "name": block.get("name")}
                    elif btype == "tool_use":
                        # CLIENT tool — terminal; forward re-indexed, buffer args to rebuild.
                        client_tool_present = True
                        gi = global_index
                        global_index += 1
                        local_to_global[idx] = gi
                        suppressed[idx] = {"kind": "client_tool", "buf": "",
                                           "id": block.get("id"), "name": block.get("name")}
                        ev2 = dict(ev); ev2["index"] = gi
                        yield _sse("content_block_start", ev2)
                    else:
                        gi = global_index
                        global_index += 1
                        local_to_global[idx] = gi
                        if btype == "text":
                            text_buf[idx] = ""
                        elif btype in ("thinking", "redacted_thinking"):
                            thinking_buf[idx] = {"thinking": "", "signature": "", "data": block.get("data")}
                        ev2 = dict(ev); ev2["index"] = gi
                        yield _sse("content_block_start", ev2)

                elif etype == "content_block_delta":
                    idx = ev.get("index", 0)
                    delta = ev.get("delta") or {}
                    dtype = delta.get("type")
                    if idx in suppressed and suppressed[idx]["kind"] == "web_search":
                        if dtype == "input_json_delta":
                            suppressed[idx]["buf"] += delta.get("partial_json", "") or ""
                        continue
                    if idx in suppressed and suppressed[idx]["kind"] == "client_tool":
                        if dtype == "input_json_delta":
                            suppressed[idx]["buf"] += delta.get("partial_json", "") or ""
                        gi = local_to_global.get(idx, idx)
                        ev2 = dict(ev); ev2["index"] = gi
                        yield _sse("content_block_delta", ev2)
                        continue
                    if dtype == "text_delta" and idx in text_buf:
                        text_buf[idx] += delta.get("text", "") or ""
                    elif dtype == "thinking_delta" and idx in thinking_buf:
                        thinking_buf[idx]["thinking"] += delta.get("thinking", "") or ""
                    elif dtype == "signature_delta" and idx in thinking_buf:
                        thinking_buf[idx]["signature"] += delta.get("signature", "") or ""
                    gi = local_to_global.get(idx, idx)
                    ev2 = dict(ev); ev2["index"] = gi
                    yield _sse("content_block_delta", ev2)

                elif etype == "content_block_stop":
                    idx = ev.get("index", 0)
                    if idx in suppressed and suppressed[idx]["kind"] == "web_search":
                        s = suppressed[idx]
                        try:
                            tool_input = json.loads(s["buf"]) if s["buf"] else {}
                        except (ValueError, TypeError):
                            tool_input = {}
                        pending_searches.append({"id": s["id"], "name": s["name"], "input": tool_input})
                        assistant_content.append(
                            {"type": "tool_use", "id": s["id"], "name": s["name"], "input": tool_input}
                        )
                        continue  # suppress
                    if idx in suppressed and suppressed[idx]["kind"] == "client_tool":
                        s = suppressed[idx]
                        try:
                            tool_input = json.loads(s["buf"]) if s["buf"] else {}
                        except (ValueError, TypeError):
                            tool_input = {}
                        assistant_content.append(
                            {"type": "tool_use", "id": s["id"], "name": s["name"], "input": tool_input}
                        )
                        gi = local_to_global.get(idx, idx)
                        ev2 = dict(ev); ev2["index"] = gi
                        yield _sse("content_block_stop", ev2)
                        continue
                    if idx in text_buf:
                        assistant_content.append({"type": "text", "text": text_buf[idx]})
                    elif idx in thinking_buf:
                        tb = thinking_buf[idx]
                        blk = {"type": "thinking", "thinking": tb["thinking"]}
                        if tb["signature"]:
                            blk["signature"] = tb["signature"]
                        assistant_content.append(blk)
                    gi = local_to_global.get(idx, idx)
                    ev2 = dict(ev); ev2["index"] = gi
                    yield _sse("content_block_stop", ev2)

                elif etype == "message_delta":
                    d = ev.get("delta") or {}
                    if d.get("stop_reason"):
                        stop_reason_final = d["stop_reason"]
                    u = ev.get("usage") or {}
                    merged.output_tokens += int(u.get("output_tokens", 0) or 0)
                    # captured; do NOT emit here (emitted once at envelope close)

                elif etype == "message_stop":
                    pass  # end of this turn; do not emit

                elif etype == "ping":
                    yield _sse("ping", ev)
                elif etype == "error":
                    yield _sse("error", ev)

            # ---- turn ended: decide terminal vs search ----
            is_search_turn = bool(pending_searches) and not client_tool_present
            if not is_search_turn or force_final:
                # Terminal: close the single envelope.
                yield _sse(
                    "message_delta",
                    {"type": "message_delta",
                     "delta": {"stop_reason": stop_reason_final, "stop_sequence": None},
                     "usage": {"output_tokens": merged.output_tokens}},
                )
                yield _sse("message_stop", {"type": "message_stop"})
                break

            # Search turn: run ALL requested searches (F-3) → one tool_result per tool_use_id,
            # in order. search_attempts guards the loop even if every search fails (F-5).
            # Per-search deadline recheck so a large fan-out can't run uncapped (round2 High-2).
            search_attempts += 1
            tool_results = []
            for ps in pending_searches:
                if time.monotonic() > deadline:
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": ps["id"],
                         "content": "web search deadline exceeded", "is_error": True})
                    continue
                result_text, ok = await _do_search(mcp_client, ps["input"], default_max_results)
                if ok:
                    searches_done += 1
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": ps["id"],
                     "content": result_text, **({"is_error": True} if not ok else {})}
                )
            conversation = conversation + [
                {"role": "assistant", "content": assistant_content},
                {"role": "user", "content": tool_results},
            ]
    except Exception:
        logger.exception("web_search.anthropic_stream_failed")
        if envelope_open:
            yield _sse("error", {"type": "error",
                                 "error": {"type": "api_error", "message": "web search loop failed"}})
            yield _sse("message_stop", {"type": "message_stop"})
        return
    finally:
        merged.web_search_count = searches_done
        merged.total_tokens = merged.input_tokens + merged.output_tokens
        try:
            await on_usage(merged)
        except Exception:
            logger.warning("web_search.on_usage_failed")


async def _drain_error(chunk_iter: AsyncIterator[bytes], envelope_open: bool) -> AsyncIterator[bytes]:
    """Relay a provider error turn (non-200). If the envelope was already opened we
    close it cleanly; otherwise we surface the provider's error frames directly."""
    async for raw in chunk_iter:
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            continue
        yield _sse(ev.get("type", "error"), ev)
    if envelope_open:
        yield _sse("message_stop", {"type": "message_stop"})


# ════════════════════════════════════════════════════════════════════════════════
# ANTHROPIC (Messages) — non-streaming loop
# ════════════════════════════════════════════════════════════════════════════════
async def _anthropic_nonstream(
    *,
    invoke: InvokeFn,
    base_body: dict,
    mcp_client: AgentCoreMcpClient,
    on_usage: Callable[[TokenUsage], Awaitable[None]],
    max_iterations: int,
    deadline: float,
    default_max_results: int,
) -> JSONResponse:
    from app.providers.bedrock_adapter import _extract_bedrock_usage

    merged = TokenUsage()
    conversation: list[dict] = list(base_body.get("messages") or [])
    searches_done = 0
    search_attempts = 0      # loop guard incl. failures (F-5)
    final_status = 200
    final_body: dict = {}

    try:
        while True:
            force_final = search_attempts >= max_iterations or time.monotonic() > deadline
            turn_body = _with_web_search_tool(base_body, "anthropic", include=not force_final)
            turn_body = dict(turn_body)
            turn_body["messages"] = conversation
            turn_body.pop("stream", None)
            status, body, _h, usage = await invoke(turn_body)
            final_status = status
            try:
                final_body = json.loads(body)
            except (ValueError, TypeError):
                final_body = {"error": {"type": "api_error", "message": "invalid provider response"}}
            if status != 200:
                break
            _merge_usage(merged, usage)

            content = final_body.get("content") or []
            our_calls = [b for b in content if b.get("type") == "tool_use" and b.get("name") == GW_WEB_SEARCH_NAME]
            client_calls = [b for b in content if b.get("type") == "tool_use" and b.get("name") != GW_WEB_SEARCH_NAME]

            if force_final or not our_calls or client_calls:
                break  # terminal — return this body

            search_attempts += 1
            tool_results = []
            assistant_content = content
            for call in our_calls:
                if time.monotonic() > deadline:
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": call.get("id"),
                         "content": "web search deadline exceeded", "is_error": True})
                    continue
                result_text, ok = await _do_search(mcp_client, call.get("input") or {}, default_max_results)
                if ok:
                    searches_done += 1
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": call.get("id"),
                     "content": result_text, **({"is_error": True} if not ok else {})}
                )
            conversation = conversation + [
                {"role": "assistant", "content": assistant_content},
                {"role": "user", "content": tool_results},
            ]
    finally:
        # Fire on_usage whenever any tokens accrued — even if a LATER turn failed after
        # earlier turns succeeded (tokens were consumed and must be accounted) (F-9).
        merged.web_search_count = searches_done
        if (merged.input_tokens + merged.output_tokens) > 0:
            try:
                await on_usage(merged)
            except Exception:
                logger.warning("web_search.on_usage_failed")

    if final_status == 200 and isinstance(final_body.get("usage"), dict):
        # Overwrite the returned body's usage with the merged (multi-turn) totals so the
        # client sees the full accounting; reasoning stays a submetric.
        final_body["usage"]["input_tokens"] = merged.input_tokens
        final_body["usage"]["output_tokens"] = merged.output_tokens
    return JSONResponse(status_code=final_status, content=final_body)


# ════════════════════════════════════════════════════════════════════════════════
# RESPONSES (OpenAI) — helpers
# ════════════════════════════════════════════════════════════════════════════════
def _normalize_responses_input(body: dict) -> list:
    """Responses `input` may be a string or an array of items — normalize to a list
    so we can append function_call / function_call_output items for continuation."""
    inp = body.get("input")
    if isinstance(inp, list):
        return list(inp)
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}]
    return []


# ════════════════════════════════════════════════════════════════════════════════
# RESPONSES (OpenAI) — streaming stitcher
# ════════════════════════════════════════════════════════════════════════════════
async def _responses_stream(
    *,
    invoke_stream: InvokeStreamFn,
    base_body: dict,
    mcp_client: AgentCoreMcpClient,
    request: Request,
    on_usage: Callable[[TokenUsage], Awaitable[None]],
    max_iterations: int,
    deadline: float,
    default_max_results: int,
) -> AsyncIterator[bytes]:
    """Stitch N Responses turns into ONE response.created … response.completed stream.

    Forwards message/text output items (re-indexed); suppresses function_call plumbing
    for our web_search; runs the search between turns.
    """
    merged = TokenUsage()
    conv_input: list = _normalize_responses_input(base_body)
    searches_done = 0        # successful → web_search_count
    search_attempts = 0      # all rounds incl. failures → loop guard (F-5)
    envelope_open = False
    global_out_index = 0
    final_response_obj: Optional[dict] = None
    final_terminal_type = "response.completed"  # actual upstream terminal type (F-1)
    our_call_ids: set[str] = set()  # our web_search call_ids to strip from final output (F-3 Responses)
    error_seen = False       # a 200-stream `error` event occurred (NEW round2 High-1)

    try:
        while True:
            force_final = search_attempts >= max_iterations or time.monotonic() > deadline
            turn_body = _with_web_search_tool(base_body, "responses", include=not force_final)
            turn_body = dict(turn_body)
            turn_body["input"] = conv_input
            turn_body["stream"] = True
            status, chunk_iter, _h, _rid = await invoke_stream(turn_body)
            if status != 200:
                async for b in _drain_responses_error(chunk_iter, envelope_open):
                    yield b
                return

            local_to_global: dict[int, int] = {}
            suppressed_out: dict[int, dict] = {}     # our web_search fn call by output_index
            fn_arg_buf: dict[int, str] = {}          # output_index → args buffer (our fn)
            turn_output_items: list[dict] = []       # completed output items (rebuild conv_input)
            pending_searches: list[dict] = []         # [{call_id, input}] — many per turn (F-3)
            client_tool_present = False

            async for raw in chunk_iter:
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = ev.get("type", "")

                if etype == "response.created":
                    if not envelope_open:
                        envelope_open = True
                        yield _sse("response.created", ev)
                elif etype == "response.in_progress":
                    if not envelope_open:
                        yield _sse("response.in_progress", ev)

                elif etype == "response.output_item.added":
                    item = ev.get("item") or {}
                    oidx = ev.get("output_index", 0)
                    itype = item.get("type")
                    if itype == "function_call" and item.get("name") == GW_WEB_SEARCH_NAME:
                        suppressed_out[oidx] = {"kind": "web_search",
                                                "call_id": item.get("call_id"),
                                                "name": item.get("name")}
                        fn_arg_buf[oidx] = ""
                        if item.get("call_id"):
                            our_call_ids.add(item["call_id"])  # strip from final output (F-3 Responses)
                    elif itype == "function_call":
                        client_tool_present = True
                        gi = global_out_index; global_out_index += 1
                        local_to_global[oidx] = gi
                        ev2 = dict(ev); ev2["output_index"] = gi
                        yield _sse(etype, ev2)
                    else:
                        gi = global_out_index; global_out_index += 1
                        local_to_global[oidx] = gi
                        ev2 = dict(ev); ev2["output_index"] = gi
                        yield _sse(etype, ev2)

                elif etype == "response.function_call_arguments.delta":
                    oidx = ev.get("output_index", 0)
                    if oidx in suppressed_out:
                        fn_arg_buf[oidx] += ev.get("delta", "") or ""
                    else:
                        gi = local_to_global.get(oidx, oidx)
                        ev2 = dict(ev); ev2["output_index"] = gi
                        yield _sse(etype, ev2)

                elif etype == "response.function_call_arguments.done":
                    oidx = ev.get("output_index", 0)
                    if oidx not in suppressed_out:
                        gi = local_to_global.get(oidx, oidx)
                        ev2 = dict(ev); ev2["output_index"] = gi
                        yield _sse(etype, ev2)

                elif etype == "response.output_item.done":
                    item = ev.get("item") or {}
                    oidx = ev.get("output_index", 0)
                    turn_output_items.append(item)
                    if oidx in suppressed_out and suppressed_out[oidx]["kind"] == "web_search":
                        try:
                            args = json.loads(fn_arg_buf.get(oidx, "") or "{}")
                        except (ValueError, TypeError):
                            args = {}
                        pending_searches.append({"call_id": suppressed_out[oidx]["call_id"], "input": args})
                        continue  # suppress
                    gi = local_to_global.get(oidx, oidx)
                    ev2 = dict(ev); ev2["output_index"] = gi
                    yield _sse(etype, ev2)

                elif etype in (
                    "response.output_text.delta", "response.output_text.done",
                    "response.content_part.added", "response.content_part.done",
                    "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
                ):
                    oidx = ev.get("output_index", 0)
                    if oidx in suppressed_out:
                        continue
                    gi = local_to_global.get(oidx, oidx)
                    ev2 = dict(ev); ev2["output_index"] = gi
                    yield _sse(etype, ev2)

                elif etype in ("response.completed", "response.incomplete", "response.failed"):
                    resp_obj = ev.get("response") or {}
                    final_response_obj = resp_obj
                    final_terminal_type = etype  # preserve incomplete/failed, don't fake completed (F-1)
                    u = resp_obj.get("usage") or {}
                    in_d = u.get("input_tokens_details") or {}
                    out_d = u.get("output_tokens_details") or {}
                    merged.input_tokens += int(u.get("input_tokens", 0) or 0)
                    merged.output_tokens += int(u.get("output_tokens", 0) or 0)
                    merged.cache_read_input_tokens += int(in_d.get("cached_tokens", 0) or 0)
                    merged.reasoning_tokens += int(out_d.get("reasoning_tokens", 0) or 0)
                    # captured; emit our own terminal event at envelope close
                elif etype == "error":
                    error_seen = True  # NEW round2 High-1: do not also emit a fake completed
                    yield _sse("error", ev)

            is_search_turn = bool(pending_searches) and not client_tool_present and not error_seen
            # An incomplete/failed upstream turn is terminal even if a search was requested —
            # never loop on a truncated/failed response (F-1).
            if not is_search_turn or force_final or final_terminal_type != "response.completed" or error_seen:
                # If a mid-stream `error` occurred, the error frame is the terminal signal —
                # do NOT also emit a synthetic response.completed (NEW round2 High-1). Emit
                # response.failed only if we never got a real terminal event.
                if error_seen and final_terminal_type == "response.completed":
                    yield _sse("response.failed",
                               {"type": "response.failed",
                                "response": _finalize_responses_obj(
                                    final_response_obj, merged, global_out_index,
                                    "response.failed", our_call_ids)})
                else:
                    yield _sse(
                        final_terminal_type,
                        {"type": final_terminal_type,
                         "response": _finalize_responses_obj(
                             final_response_obj, merged, global_out_index,
                             final_terminal_type, our_call_ids)},
                    )
                break

            # Search turn: run ALL requested searches (F-3) → one function_call_output per call_id,
            # in order. search_attempts guards the loop even if every search fails (F-5).
            # Per-search deadline recheck so a fan-out of many searches can't run uncapped
            # past the total deadline (NEW round2 High-2).
            search_attempts += 1
            outputs = []
            for ps in pending_searches:
                if time.monotonic() > deadline:
                    outputs.append({"type": "function_call_output", "call_id": ps["call_id"],
                                    "output": json.dumps({"error": "web search deadline exceeded"})})
                    continue
                result_text, ok = await _do_search(mcp_client, ps["input"], default_max_results)
                if ok:
                    searches_done += 1
                outputs.append(
                    {"type": "function_call_output", "call_id": ps["call_id"], "output": result_text}
                )
            conv_input = conv_input + turn_output_items + outputs
    except Exception:
        logger.exception("web_search.responses_stream_failed")
        if envelope_open:
            # Close the already-open envelope with a terminal response.failed so the client
            # never hangs on an open response (F-6).
            yield _sse("error", {"type": "error",
                                 "error": {"type": "api_error", "message": "web search loop failed"}})
            yield _sse("response.failed",
                       {"type": "response.failed",
                        "response": _finalize_responses_obj(
                            final_response_obj, merged, global_out_index, "response.failed", our_call_ids)})
        return
    finally:
        merged.web_search_count = searches_done
        merged.total_tokens = merged.input_tokens + merged.output_tokens
        try:
            await on_usage(merged)
        except Exception:
            logger.warning("web_search.on_usage_failed")


def _finalize_responses_obj(
    resp_obj: Optional[dict], merged: TokenUsage, _n: int,
    terminal_type: str = "response.completed",
    our_call_ids: Optional[set] = None,
) -> dict:
    """Build the terminal response object with merged usage (multi-turn totals).

    Preserves the real upstream terminal status: response.completed → 'completed',
    response.incomplete → 'incomplete', response.failed → 'failed' (F-1).
    Strips OUR web_search function_call items from `output` so the client's final
    response never contains a function_call it was never streamed (F-3 Responses).
    """
    status_map = {"response.completed": "completed",
                  "response.incomplete": "incomplete",
                  "response.failed": "failed"}
    obj = dict(resp_obj or {})
    obj["status"] = status_map.get(terminal_type, "completed")
    if our_call_ids and isinstance(obj.get("output"), list):
        obj["output"] = [
            it for it in obj["output"]
            if not (isinstance(it, dict) and it.get("type") == "function_call"
                    and it.get("call_id") in our_call_ids)
        ]
    obj["usage"] = {
        "input_tokens": merged.input_tokens,
        "output_tokens": merged.output_tokens,
        "total_tokens": merged.input_tokens + merged.output_tokens,
        "input_tokens_details": {"cached_tokens": merged.cache_read_input_tokens},
        "output_tokens_details": {"reasoning_tokens": merged.reasoning_tokens},
    }
    return obj


async def _drain_responses_error(chunk_iter: AsyncIterator[bytes], envelope_open: bool) -> AsyncIterator[bytes]:
    async for raw in chunk_iter:
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            continue
        yield _sse(ev.get("type", "error"), ev)
    # If a LATER turn returned non-200 after the envelope was already opened, close it with a
    # terminal response.failed so the client doesn't hang on an open response (F-6).
    if envelope_open:
        yield _sse("response.failed",
                   {"type": "response.failed", "response": {"status": "failed"}})


# ════════════════════════════════════════════════════════════════════════════════
# RESPONSES (OpenAI) — non-streaming loop
# ════════════════════════════════════════════════════════════════════════════════
async def _responses_nonstream(
    *,
    invoke: InvokeFn,
    base_body: dict,
    mcp_client: AgentCoreMcpClient,
    on_usage: Callable[[TokenUsage], Awaitable[None]],
    max_iterations: int,
    deadline: float,
    default_max_results: int,
) -> JSONResponse:
    merged = TokenUsage()
    conv_input: list = _normalize_responses_input(base_body)
    searches_done = 0
    search_attempts = 0      # loop guard incl. failures (F-5)
    final_status = 200
    final_body: dict = {}

    try:
        while True:
            force_final = search_attempts >= max_iterations or time.monotonic() > deadline
            turn_body = _with_web_search_tool(base_body, "responses", include=not force_final)
            turn_body = dict(turn_body)
            turn_body["input"] = conv_input
            turn_body.pop("stream", None)
            status, body, _h, usage = await invoke(turn_body)
            final_status = status
            try:
                final_body = json.loads(body)
            except (ValueError, TypeError):
                final_body = {"error": {"type": "api_error", "message": "invalid provider response"}}
            if status != 200:
                break
            _merge_usage(merged, usage)

            output = final_body.get("output") or []
            our_calls = [o for o in output if o.get("type") == "function_call" and o.get("name") == GW_WEB_SEARCH_NAME]
            client_calls = [o for o in output if o.get("type") == "function_call" and o.get("name") != GW_WEB_SEARCH_NAME]

            if force_final or not our_calls or client_calls:
                break

            search_attempts += 1
            new_items = list(output)
            for call in our_calls:
                if time.monotonic() > deadline:
                    new_items.append({"type": "function_call_output", "call_id": call.get("call_id"),
                                      "output": json.dumps({"error": "web search deadline exceeded"})})
                    continue
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                result_text, ok = await _do_search(mcp_client, args, default_max_results)
                if ok:
                    searches_done += 1
                new_items.append(
                    {"type": "function_call_output", "call_id": call.get("call_id"), "output": result_text}
                )
            conv_input = conv_input + new_items
    finally:
        merged.web_search_count = searches_done  # fire on any accrued usage even on late failure (F-9)
        if (merged.input_tokens + merged.output_tokens) > 0:
            try:
                await on_usage(merged)
            except Exception:
                logger.warning("web_search.on_usage_failed")

    if final_status == 200 and isinstance(final_body.get("usage"), dict):
        final_body["usage"]["input_tokens"] = merged.input_tokens
        final_body["usage"]["output_tokens"] = merged.output_tokens
        final_body["usage"]["total_tokens"] = merged.input_tokens + merged.output_tokens
    return JSONResponse(status_code=final_status, content=final_body)


# ════════════════════════════════════════════════════════════════════════════════
# public dispatcher
# ════════════════════════════════════════════════════════════════════════════════
async def run_web_search_loop(
    *,
    dialect: str,
    invoke: InvokeFn,
    invoke_stream: InvokeStreamFn,
    initial_req_data: dict,
    is_stream: bool,
    mcp_client: AgentCoreMcpClient,
    request: Request,
    on_usage: Callable[[TokenUsage], Awaitable[None]],
    max_iterations: int = 5,
    total_deadline_sec: float = 90.0,
    default_max_results: int = 10,
    response_headers: Optional[dict] = None,
) -> StreamingResponse | JSONResponse:
    """Run the server-side web-search loop and return the client response.

    ``dialect`` is "anthropic" (/v1/messages) or "responses" (/v1/responses). The loop
    ensures the MCP client is initialized (discovers the WebSearch tool) before starting;
    if that fails, it degrades to a plain pass-through of the original request (no tool).
    """
    deadline = time.monotonic() + total_deadline_sec

    # streaming.py sse helpers now call on_usage(usage, first_token_time) (2-arg TTFT
    # contract). The web-search loop's on_usage is 1-arg (multi-turn aggregate — per-turn
    # TTFT is not meaningful), so drop the first_token_time when threading the callback
    # into a pass-through sse helper.
    async def _stream_on_usage(usage: TokenUsage, _first_token_time: float | None = None) -> None:
        await on_usage(usage)

    # F-7: if the client already declared its OWN tool named `web_search`, do not hijack it.
    # Skip the loop entirely and pass the request through unmodified (client's tool loop runs).
    if _client_declares_web_search(initial_req_data):
        logger.info("web_search.client_owns_tool_skip")
        base = dict(initial_req_data)
        if is_stream:
            base["stream"] = True
            status, chunk_iter, _h, _ = await invoke_stream(base)
            from app.services.streaming import (
                bedrock_anthropic_sse_stream,
                responses_sse_stream,
            )
            gen = (bedrock_anthropic_sse_stream if dialect == "anthropic" else responses_sse_stream)(
                request, chunk_iter, on_usage=_stream_on_usage)
            return StreamingResponse(gen, status_code=status,
                                     media_type="text/event-stream", headers=response_headers)
        base.pop("stream", None)
        status, body, _h, usage = await invoke(base)
        if usage and (usage.input_tokens + usage.output_tokens) > 0:
            await on_usage(usage)
        try:
            content = json.loads(body)
        except (ValueError, TypeError):
            content = {"error": {"type": "api_error", "message": "invalid provider response"}}
        return JSONResponse(status_code=status, content=content, headers=response_headers)

    # Ensure the AgentCore WebSearch tool is discoverable before we advertise it to the
    # model. If discovery fails, fall back to a normal (no-search) call so the request
    # still succeeds — the model simply lacks web search this time.
    try:
        await mcp_client.ensure_initialized()
    except Exception:
        logger.warning("web_search.mcp_init_failed_fallback_no_search")
        # Degrade to a normal (no-search) single turn. Route the stream through the real
        # dialect SSE helper so usage is aggregated and on_usage still fires (F-4) — a
        # successful no-search response must NOT lose cost accounting.
        base = dict(initial_req_data)
        base.pop("stream", None)
        if is_stream:
            base["stream"] = True
            status, chunk_iter, headers, _ = await invoke_stream(base)
            from app.services.streaming import (
                bedrock_anthropic_sse_stream,
                responses_sse_stream,
            )
            if dialect == "anthropic":
                gen = bedrock_anthropic_sse_stream(request, chunk_iter, on_usage=_stream_on_usage)
            else:
                gen = responses_sse_stream(request, chunk_iter, on_usage=_stream_on_usage)
            return StreamingResponse(gen, status_code=status,
                                     media_type="text/event-stream", headers=response_headers)
        status, body, _h, usage = await invoke(base)
        if usage and (usage.input_tokens + usage.output_tokens) > 0:
            await on_usage(usage)
        try:
            content = json.loads(body)
        except (ValueError, TypeError):
            content = {"error": {"type": "api_error", "message": "invalid provider response"}}
        return JSONResponse(status_code=status, content=content, headers=response_headers)

    if is_stream:
        stitcher = _anthropic_stream if dialect == "anthropic" else _responses_stream
        gen = stitcher(
            invoke_stream=invoke_stream,
            base_body=initial_req_data,
            mcp_client=mcp_client,
            request=request,
            on_usage=on_usage,
            max_iterations=max_iterations,
            deadline=deadline,
            default_max_results=default_max_results,
        )
        return StreamingResponse(
            gen, status_code=200, media_type="text/event-stream", headers=response_headers
        )

    loop = _anthropic_nonstream if dialect == "anthropic" else _responses_nonstream
    return await loop(
        invoke=invoke,
        base_body=initial_req_data,
        mcp_client=mcp_client,
        on_usage=on_usage,
        max_iterations=max_iterations,
        deadline=deadline,
        default_max_results=default_max_results,
    )
