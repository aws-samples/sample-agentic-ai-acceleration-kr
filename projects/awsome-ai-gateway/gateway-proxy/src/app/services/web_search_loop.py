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

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Optional

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.providers.openai_usage import extract_responses_usage
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


#: force_final 턴에서 우리 tool_use 를 치환할 때 쓰는 안내. 모델이 "도구를 더 쓸 수 없고
#: 이미 받은 결과로 답해야 한다" 는 것을 알아야 한다 — 그냥 지우면 검색을 했다는 사실 자체가
#: 사라져서 모델이 "검색할 수 없었다" 고 답할 수 있다.
_FINAL_TURN_TOOL_NOTE = "[web search results provided below; no further searches available]"


def _strip_anthropic_web_search_plumbing(
    messages: list, our_tool_use_ids: set[str]
) -> list:
    """force_final 턴을 위해 **우리** web_search 배관을 대화에서 걷어낸다.

    왜 필요한가
    -----------
    force_final 턴(``max_iterations`` 소진 또는 deadline 초과)은 ``tools`` 키를 아예
    빼고 보낸다 — 더 검색하지 않겠다는 뜻이다. 그런데 대화에는 앞선 턴이 쌓아 둔
    ``tool_use`` / ``tool_result`` 블록이 그대로 남아 있다. Anthropic-on-Bedrock 은
    **tool_use/tool_result 를 담은 요청이 tools 를 정의하지 않으면 거부**한다.

    그 결과가 최악의 형태다: 마지막 턴만 400 이 되어, 이미 과금된 N 번의 모델 턴과 N 번의
    검색이 전부 버려지고 사용자는 답을 하나도 받지 못한다. deadline 경로에서는 검색 한 번만
    있어도 재현된다.

    무엇으로 바꾸나
    ---------------
    ``tool_result`` 는 **텍스트 블록으로 변환**한다 — 그 안의 웹 결과는 이미 비용을 지불한
    것이고, 지우면 모델이 근거 없이 답하게 된다. 우리 ``tool_use`` 는 안내 텍스트로
    치환한다(그냥 지우면 assistant 메시지가 비는데, 빈 content 도 거부된다).

    클라이언트 소유 도구의 ``tool_use``/``tool_result`` 는 **건드리지 않는다** —
    ``our_tool_use_ids`` 에 없는 것은 그대로 둔다. 그쪽은 클라이언트가 자기 루프에서 쓰는
    것이고, 애초에 client_tool_present 면 이 루프가 그 턴에서 끝난다.

    우리 id 가 하나도 없으면 **입력 객체를 그대로 돌려준다**(사본도 만들지 않는다) — 검색이
    없었던 요청은 바이트 단위로 동일한 경로를 타야 한다.
    """
    if not our_tool_use_ids:
        return messages

    out: list = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue

        new_content: list = []
        changed = False
        for block in content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue
            btype = block.get("type")
            if btype == "tool_use" and block.get("id") in our_tool_use_ids:
                changed = True
                continue  # 아래에서 비면 안내 텍스트로 채운다
            if btype == "tool_result" and block.get("tool_use_id") in our_tool_use_ids:
                changed = True
                raw = block.get("content")
                if isinstance(raw, list):
                    # content 가 블록 배열인 형태 — 텍스트만 이어붙인다.
                    text = "".join(
                        b.get("text", "") for b in raw if isinstance(b, dict)
                    )
                else:
                    text = raw if isinstance(raw, str) else json.dumps(raw)
                new_content.append({"type": "text", "text": text})
                continue
            new_content.append(block)

        if not changed:
            out.append(msg)
            continue
        if not new_content:
            # ⚠️ 빈 content 는 거부된다. 우리 tool_use 하나만 있던 assistant 메시지가
            #    정확히 이 경우다.
            new_content = [{"type": "text", "text": _FINAL_TURN_TOOL_NOTE}]
        out.append({**msg, "content": new_content})
    return out


def _strip_responses_web_search_items(input_items: list, our_call_ids: set[str]) -> list:
    """Responses 방언의 같은 작업.

    ``function_call`` / ``function_call_output`` 쌍을 걷어내고, 출력은 텍스트 메시지로
    바꾼다(같은 이유 — 이미 지불한 검색 결과다).

    ⚠️ 제거되는 ``function_call`` **직전의 ``reasoning`` 항목도 함께 지운다.** Responses
       API 는 뒤따르는 쌍이 없는 reasoning 항목을 거부하므로, 배관만 지우면 그 reasoning 이
       고아가 되어 다시 400 이 된다.
    """
    if not our_call_ids:
        return input_items

    out: list = []
    for item in input_items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        itype = item.get("type")
        if itype == "function_call" and item.get("call_id") in our_call_ids:
            # 직전 reasoning 항목이 이 호출에 딸린 것이면 함께 제거한다.
            if out and isinstance(out[-1], dict) and out[-1].get("type") == "reasoning":
                out.pop()
            continue
        if itype == "function_call_output" and item.get("call_id") in our_call_ids:
            raw = item.get("output")
            text = raw if isinstance(raw, str) else json.dumps(raw)
            out.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
            continue
        out.append(item)
    return out


def _is_our_tool(tool: dict) -> bool:
    return isinstance(tool, dict) and tool.get("name") == GW_WEB_SEARCH_NAME


# Anthropic 블록 / Responses 항목 중 **클라이언트가 실행해야 하는** 도구 호출 판정.
#
# ⚠️ 예전에는 각각 정확히 ``tool_use`` / ``function_call`` 만 셌다. 두 방언 모두 그것이
#    도구 호출 항목의 전부가 아니다 — Responses 는 커스텀(freeform) 도구를
#    ``custom_tool_call`` 로, 로컬 실행 도구를 ``local_shell_call`` / ``computer_call`` 로
#    보내고, Anthropic 은 ``server_tool_use`` / ``mcp_tool_use`` 를 쓴다.
#
#    이 판정이 False 가 되면 ``is_search_turn`` 이 True 가 되어 게이트웨이가 검색을 돌리고
#    루프를 한 바퀴 더 돈다 — 그 과정에서 **클라이언트의 도구 호출이 삼켜진다.** 클라이언트는
#    자기가 실행해야 할 호출을 보지 못한 채 기다린다. 모델이 우리 web_search 와 자기 도구를
#    같은 턴에 함께 호출하면(두 방언 모두 병렬 도구 호출을 지원한다) 바로 재현된다.
#
#    그래서 화이트리스트가 아니라 **접미사**로 판정한다: 새 호출 유형이 생겼을 때 삼키는
#    쪽이 아니라 넘겨주는 쪽으로 틀리는 것이 안전하다(최악의 경우 한 턴 일찍 끝난다).
def _is_client_tool_use_block(block: dict) -> bool:
    """Anthropic: 우리 것이 아닌 도구 사용 블록인가."""
    if not isinstance(block, dict):
        return False
    btype = block.get("type")
    if not isinstance(btype, str) or "tool_use" not in btype:
        return False
    return block.get("name") != GW_WEB_SEARCH_NAME


def _is_client_tool_call_item(item: dict, our_call_ids: set[str] | None = None) -> bool:
    """Responses: 우리 것이 아닌 도구 호출 항목인가."""
    if not isinstance(item, dict):
        return False
    itype = item.get("type")
    if not isinstance(itype, str) or not itype.endswith("_call"):
        return False
    if item.get("name") == GW_WEB_SEARCH_NAME:
        return False
    if our_call_ids and item.get("call_id") in our_call_ids:
        return False
    return True


def _client_declares_web_search(body: dict) -> bool:
    """True if the client's ORIGINAL request already declares a tool named web_search.

    If so we must NOT inject/hijack it (F-7) — the loop is skipped and the request passes
    through so the client's own tool loop runs unmodified.
    """
    for t in (body.get("tools") or []):
        if isinstance(t, dict) and t.get("name") == GW_WEB_SEARCH_NAME:
            return True
    return False


# Anthropic/OpenAI NATIVE server-side web_search tools carry a `type` naming the
# server tool (Messages: "web_search_20250305"; Responses: "web_search_preview").
# Bedrock/Mantle reject these ("tool type ... is not supported for this model").
# We STRIP them and fulfill the intent with our own injected tool + search loop.
# A genuinely custom client tool merely NAMED web_search has no such type and is
# left alone (F-7). Clients like Claude Code CLI attach the native tool by default.
def _is_native_web_search(tool: dict) -> bool:
    if not isinstance(tool, dict):
        return False
    t = tool.get("type")
    return isinstance(t, str) and t.startswith("web_search")


def _strip_native_web_search(body: dict) -> dict:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return body
    filtered = [t for t in tools if not _is_native_web_search(t)]
    if len(filtered) == len(tools):
        return body  # unchanged — no native tool present
    out = dict(body)
    if filtered:
        out["tools"] = filtered
    else:
        out.pop("tools", None)
    return out


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


def _wire_input(usage: TokenUsage) -> int:
    """Billing buckets → the cache-INCLUSIVE prompt count the OpenAI wires report.

    The inverse of ``split_openai_input``: TokenUsage keeps the three prompt buckets
    mutually exclusive for costing, while the client (Codex CLI reads this to track its
    context window) expects the grand total with both cache buckets folded in. Kept as one
    function because the streaming and non-streaming loops both rewrite usage on the way
    out and must agree.
    """
    return (
        usage.input_tokens
        + usage.cache_read_input_tokens
        + usage.cache_creation_input_tokens
    )


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


# ── search execution (shared) ─────────────────────────────────────────────────
def _truncate_result(text: str, max_chars: int) -> tuple[str, bool]:
    """검색 결과 텍스트를 상한까지 자른다. ``(text, truncated)``.

    ⚠️ 왜 필요한가: ``max_iterations`` 는 **턴 수**를, ``total_deadline_sec`` 는 **시간**을
       묶는다. 청구서를 결정하는 두 축 — 다음 턴 입력에 주입되는 **바이트 수**와 한 턴의
       **검색 횟수** — 는 어느 것도 묶이지 않았다. dev 실측: 검색 **한 번**이 다음 턴
       입력에 약 17.4K 토큰의 원본 결과 텍스트를 넣었다. 모델이 한 턴에 20개의 병렬
       web_search 를 내보내면(이 루프는 그것을 의도적으로 지원한다) 20 × 17.4K 가 다음
       턴 입력에 연결된다. 결과는 둘 중 하나다: 공유 예산에 상한 없는 단일 요청 비용, 또는
       컨텍스트 창을 넘겨 continuation 턴이 400 이 되면서 **그때까지 과금된 모든 턴이
       버려지는** 것.

    ⚠️ 잘랐다는 표지를 반드시 붙인다. 조용한 절단이 최악이다 — JSON 이 레코드 중간에서
       끊긴 것을 모델은 "결과 전체" 로 읽고 단정적으로 답한다.

    ``max_chars <= 0`` 이면 캡을 끄고 입력을 그대로 돌려준다(캡 이전 동작이 바이트 단위로
    재현 가능해야 한다).
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    marker = "\n\n[truncated by gateway: result set exceeded the per-search size cap]"
    return text[:max_chars] + marker, True


def _turn_search_allowance(requested: int, max_per_turn: int) -> int:
    """한 턴에서 실제로 실행할 검색 개수. ``max_per_turn <= 0`` 이면 무제한.

    ⚠️ 초과분도 **응답은 만들어 줘야 한다.** Anthropic/Responses 는 tool_use 하나당
       정확히 하나의 tool_result / function_call_output 을 요구한다 — 개수가 어긋나면
       다음 턴이 400 이다. 그래서 "실행하지 않는다" 와 "결과를 만들지 않는다" 는 다르다.
    """
    if max_per_turn <= 0:
        return requested
    return min(requested, max_per_turn)


async def _do_search(
    mcp_client: AgentCoreMcpClient, tool_input: dict, default_max: int,
    max_result_chars: int = 0,
) -> tuple[str, bool]:
    """Run one web search. Returns (result_text_for_model, ok). Never raises — on
    failure returns an error string so the model can continue from its own knowledge.

    캡을 **여기서** 적용한다 — 네 개 경로(anthropic/responses × 스트리밍/비스트리밍)가
    결과 텍스트를 얻는 유일한 지점이라, 여기 두면 넷이 갈라질 수 없다.

    ⚠️ 잘린 검색도 ``ok=True`` 를 유지한다. 그 쿼리는 청구됐고 답변을 실제로 근거지었다 —
       캡이 성공 플래그를 뒤집으면 web_search_count(과금/귀속)가 어긋난다.
    """
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
        text, truncated = _truncate_result(resp.raw_text, max_result_chars)
        if truncated:
            logger.info(
                "web_search.result_truncated",
                original_chars=len(resp.raw_text),
                cap=max_result_chars,
            )
        return text, True
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
    max_result_chars: int = 0,
    max_searches_per_turn: int = 0,
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
    #: 200 스트림 도중 provider 오류가 왔는지. 왔으면 정상 종료를 **주장하지 않는다**.
    error_seen = False
    #: 클라이언트에 열어 준 뒤 아직 닫지 않은 content_block 인덱스. 상류가 중간에 죽으면
    #: 이걸 닫아 줘야 SDK 의 파서 상태가 정리된다(열린 채 끝나면 파싱 오류로 보인다).
    open_global_blocks: set[int] = set()
    #: ⚠️ stop_reason 과 "종료 프레임을 봤는지" 는 **턴 단위** 상태다. 루프 스코프에 두면
    #:    검색 턴의 stop_reason("tool_use")이, 다음 턴이 잘렸을 때 그대로 최종 프레임으로
    #:    새어 나간다. 클라이언트에는 tool_use 블록이 하나도 보이지 않았는데(우리 것은 전부
    #:    억제된다) stop_reason 이 tool_use 이면 Anthropic SDK/Claude Code 는 도구 결과를
    #:    기다리며 없는 tool_use 를 찾다가 멈추거나 재요청한다.
    stop_reason_final = "end_turn"
    saw_message_delta = False
    #: 우리가 주입한 web_search 의 tool_use id 전체. force_final 턴에서 이 배관을
    #: 걷어내야 tools 없는 요청이 400 이 되지 않는다(_strip_… docstring 참조).
    our_tool_use_ids: set[str] = set()

    try:
        while True:
            force_final = search_attempts >= max_iterations or time.monotonic() > deadline
            turn_body = _with_web_search_tool(base_body, "anthropic", include=not force_final)
            turn_body = dict(turn_body)
            # ⚠️ force_final 턴은 `tools` 키를 아예 뺀다. 그런데 대화에는 앞선 턴이 쌓아 둔
            #    우리 tool_use/tool_result 가 남아 있고, Anthropic-on-Bedrock 은 tools 를
            #    정의하지 않은 요청에 그 블록들이 있으면 **거부한다**. 그러면 마지막 턴만
            #    400 이 되어 이미 과금된 N 턴과 N 번의 검색이 전부 버려진다.
            turn_body["messages"] = (
                _strip_anthropic_web_search_plumbing(conversation, our_tool_use_ids)
                if force_final
                else conversation
            )
            turn_body["stream"] = True

            status, chunk_iter, _headers, _rid = await invoke_stream(turn_body)
            if status != 200:
                async for b in _drain_error(chunk_iter, envelope_open):
                    yield b
                return

            # Per-turn parse state.
            # ⚠️ stop_reason / saw_message_delta 를 턴마다 재설정한다(선언부 주석의 이유).
            stop_reason_final = "end_turn"
            saw_message_delta = False
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
                    elif _is_client_tool_use_block(block):
                        # CLIENT tool — terminal; forward re-indexed, buffer args to rebuild.
                        client_tool_present = True
                        gi = global_index
                        global_index += 1
                        local_to_global[idx] = gi
                        suppressed[idx] = {"kind": "client_tool", "buf": "",
                                           "id": block.get("id"), "name": block.get("name")}
                        ev2 = dict(ev); ev2["index"] = gi
                        open_global_blocks.add(gi)
                        yield _sse("content_block_start", ev2)
                    elif btype in ("thinking", "redacted_thinking"):
                        # Buffer thinking for the INTERNAL conversation (the next model turn's
                        # same-model replay needs it), but do NOT emit it to the client stream
                        # and do NOT advance global_index. Stitching merges N turns into one
                        # envelope; a thinking block replayed inside a stitched assistant message
                        # is rejected by Bedrock ("thinking blocks ... cannot be modified").
                        thinking_buf[idx] = {"kind": btype, "thinking": "", "signature": "",
                                             "data": block.get("data")}
                    else:
                        gi = global_index
                        global_index += 1
                        local_to_global[idx] = gi
                        if btype == "text":
                            text_buf[idx] = ""
                        ev2 = dict(ev); ev2["index"] = gi
                        open_global_blocks.add(gi)
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
                    if idx in thinking_buf:
                        # Accumulate thinking/signature internally; never emit to client.
                        if dtype == "thinking_delta":
                            thinking_buf[idx]["thinking"] += delta.get("thinking", "") or ""
                        elif dtype == "signature_delta":
                            thinking_buf[idx]["signature"] += delta.get("signature", "") or ""
                        continue
                    if dtype == "text_delta" and idx in text_buf:
                        text_buf[idx] += delta.get("text", "") or ""
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
                        if s["id"]:
                            our_tool_use_ids.add(s["id"])
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
                        open_global_blocks.discard(gi)
                        yield _sse("content_block_stop", ev2)
                        continue
                    if idx in thinking_buf:
                        # Keep the thinking block in the INTERNAL conversation so the next
                        # model turn's same-model replay is valid; do NOT emit its stop to
                        # the client (start/delta were already suppressed above).
                        tb = thinking_buf[idx]
                        if tb.get("kind") == "redacted_thinking":
                            blk = {"type": "redacted_thinking", "data": tb.get("data")}
                        else:
                            blk = {"type": "thinking", "thinking": tb["thinking"]}
                            if tb["signature"]:
                                blk["signature"] = tb["signature"]
                        assistant_content.append(blk)
                        continue  # suppress from client
                    if idx in text_buf:
                        assistant_content.append({"type": "text", "text": text_buf[idx]})
                    gi = local_to_global.get(idx, idx)
                    ev2 = dict(ev); ev2["index"] = gi
                    open_global_blocks.discard(gi)
                    yield _sse("content_block_stop", ev2)

                elif etype == "message_delta":
                    saw_message_delta = True
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
                    error_seen = True
                    yield _sse("error", ev)
                elif isinstance(ev.get("error"), dict):
                    # ⚠️ 이 레포의 **모든** 어댑터가 내보내는 오류 청크는 type 이
                    #    중첩되어 있다: {"error": {"type": "provider_error", ...}}.
                    #    최상위 "type" 이 없으므로 위 `etype == "error"` 분기에 걸리지
                    #    않고, 그대로 아무 분기도 타지 않아 **조용히 사라졌다.** 결과는
                    #    잘린 답변에 붙은 정상 종료 프레임 — 클라이언트도 감사 로그도
                    #    "성공" 으로 기록한다(mantle_adapter 는 200 이후 스트림이 끊길 때
                    #    이 청크를 중간에 흘린다).
                    #
                    #    최상위 "type" 을 붙여서 다시 프레이밍한다 — SSE 이벤트 이름으로
                    #    분기하는 SDK 들이 실제로 예외를 던지게 하는 유일한 형태다.
                    error_seen = True
                    inner = ev["error"]
                    yield _sse("error", {"type": "error", "error": inner})

            # ---- turn ended: decide terminal vs search ----
            # ⚠️ 오류가 온 턴은 검색 턴으로 취급하지 않는다. 그러지 않으면 실패한 턴을
            #    "검색을 요청했다" 로 읽고 루프를 계속 돌린다.
            is_search_turn = (
                bool(pending_searches) and not client_tool_present and not error_seen
            )
            if not is_search_turn or force_final:
                # 상류가 중간에 죽어 열린 채 남은 블록을 닫는다 — 열린 채 끝나면 SDK 쪽에서
                # 파싱 오류로 보이고, 원인이 게이트웨이인지 상류인지 구분되지 않는다.
                for gi in sorted(open_global_blocks):
                    yield _sse("content_block_stop", {"type": "content_block_stop", "index": gi})
                open_global_blocks.clear()

                if error_seen or not saw_message_delta:
                    # ⚠️ 정상 종료를 **주장하지 않는다.** message_delta 는 stop_reason 을
                    #    실어 "이렇게 끝났다" 고 말하는 프레임이다. 응답이 잘렸는데 그것을
                    #    보내면 클라이언트와 감사 로그가 모두 성공으로 기록한다. 오류
                    #    프레임(위에서 이미 emit)이 종료 신호이고, message_stop 은 스트림을
                    #    닫기 위해서만 보낸다.
                    #
                    #    saw_message_delta 가 False 인 경우도 같다 — 종료 프레임을 못 받았고
                    #    (소켓 절단/타임아웃) 우리가 그것을 지어낼 근거가 없다.
                    if not error_seen:
                        yield _sse(
                            "error",
                            {"type": "error",
                             "error": {"type": "incomplete_stream",
                                       "message": "upstream ended without a terminal event"}},
                        )
                    # ⚠️ 봉투를 한 번도 열지 않았으면(message_start 미전송) message_stop 을
                    #    보내지 않는다. Anthropic SSE 계약은 message_start → … →
                    #    message_stop 이고, start 없는 stop 은 SDK 파싱 오류가 된다 — 상류
                    #    오류가 게이트웨이 버그처럼 보인다. 그때 종료 신호는 위 error
                    #    프레임이다. _drain_error 와 아래 except 절은 이미 이 구분을 한다.
                    if envelope_open:
                        yield _sse("message_stop", {"type": "message_stop"})
                    break

                # ⚠️ 클라이언트가 볼 수 없는 tool_use 로 끝났다고 말하지 않는다. 우리 검색의
                #    tool_use 블록은 전부 억제되므로, client_tool_present 가 아닌데
                #    stop_reason 이 tool_use 면 클라이언트는 없는 도구 호출을 기다린다.
                emitted_stop_reason = stop_reason_final
                if emitted_stop_reason == "tool_use" and not client_tool_present:
                    emitted_stop_reason = "end_turn"

                # Terminal: close the single envelope.
                #
                # ⚠️ usage 에 **입력 토큰도** 싣는다. message_start 는 첫 턴에서 한 번만
                #    나가므로 그 프레임의 input_tokens 는 1턴치다. N 턴을 돈 요청에서
                #    클라이언트(Claude Code 는 이 값으로 컨텍스트를 추적한다)와 감사 로그는
                #    실제로 소비한 입력의 일부만 보게 된다 — 우리가 청구하는 양과도 어긋난다.
                #    이미 보낸 message_start 를 되돌릴 수는 없으니, 마지막 프레임이 합계를
                #    말해 준다. 캐시 버킷도 함께 실어야 input_tokens 와 details 가 서로
                #    모순되지 않는다.
                if envelope_open:
                    yield _sse(
                        "message_delta",
                        {"type": "message_delta",
                         "delta": {"stop_reason": emitted_stop_reason, "stop_sequence": None},
                         "usage": {
                             "input_tokens": merged.input_tokens,
                             "output_tokens": merged.output_tokens,
                             "cache_creation_input_tokens": merged.cache_creation_input_tokens,
                             "cache_read_input_tokens": merged.cache_read_input_tokens,
                         }},
                    )
                    yield _sse("message_stop", {"type": "message_stop"})
                break

            # Search turn: run ALL requested searches (F-3) → one tool_result per tool_use_id,
            # in order. search_attempts guards the loop even if every search fails (F-5).
            # Per-search deadline recheck so a large fan-out can't run uncapped (round2 High-2).
            search_attempts += 1
            tool_results = []
            # ⚠️ 턴당 검색 개수 상한. 초과분도 **응답은 만들어 준다** — tool_use 하나당
            #    tool_result 하나가 없으면 다음 턴이 400 이므로, "실행하지 않는다" 와
            #    "결과를 만들지 않는다" 는 구별해야 한다.
            allowance = _turn_search_allowance(len(pending_searches), max_searches_per_turn)
            if allowance < len(pending_searches):
                logger.info(
                    "web_search.turn_fanout_capped",
                    requested=len(pending_searches), allowed=allowance,
                )
            for i, ps in enumerate(pending_searches):
                if i >= allowance:
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": ps["id"],
                         "content": json.dumps({
                             "error": "per-turn web search limit reached; "
                                      "answer from the results already provided"}),
                         "is_error": True})
                    continue
                if time.monotonic() > deadline:
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": ps["id"],
                         "content": "web search deadline exceeded", "is_error": True})
                    continue
                result_text, ok = await _do_search(
                    mcp_client, ps["input"], default_max_results, max_result_chars
                )
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
    max_result_chars: int = 0,
    max_searches_per_turn: int = 0,
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
            our_calls = [
                b for b in content
                if isinstance(b, dict)
                and b.get("type") == "tool_use"
                and b.get("name") == GW_WEB_SEARCH_NAME
            ]
            client_calls = [b for b in content if _is_client_tool_use_block(b)]

            if force_final or not our_calls or client_calls:
                break  # terminal — return this body

            search_attempts += 1
            tool_results = []
            assistant_content = content
            # 턴당 검색 개수 상한 — 근거는 스트리밍 스티처의 같은 주석 참조.
            allowance = _turn_search_allowance(len(our_calls), max_searches_per_turn)
            if allowance < len(our_calls):
                logger.info("web_search.turn_fanout_capped",
                            requested=len(our_calls), allowed=allowance)
            for i, call in enumerate(our_calls):
                if i >= allowance:
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": call.get("id"),
                         "content": json.dumps({
                             "error": "per-turn web search limit reached; "
                                      "answer from the results already provided"}),
                         "is_error": True})
                    continue
                if time.monotonic() > deadline:
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": call.get("id"),
                         "content": "web search deadline exceeded", "is_error": True})
                    continue
                result_text, ok = await _do_search(
                    mcp_client, call.get("input") or {}, default_max_results, max_result_chars
                )
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
        merged.total_tokens = merged.input_tokens + merged.output_tokens
        # ⚠️ 토큰이 0 이어도 **부른다.** 예전에는 `> 0` 조건이 걸려 있어서, 첫 턴이 상류
        #    4xx/5xx 로 죽으면(토큰 0) on_usage 가 아예 불리지 않았다. 그 콜백이
        #    ``cost_recorder.finalize`` 이고, finalize 의 zero-usage 경로가 RPM/TPM/비용
        #    예약을 되돌리는 **유일한** 지점이다. 이 경로는 폴백 루프를 타지 않으므로
        #    ``release_reservations`` 도 돌지 않는다 — 즉 400 을 받은 요청이 한 푼도 쓰지
        #    않고 사용자의 분/시간 한도를 창이 끝날 때까지 물고 있었다.
        #
        #    토큰이 0 이면 finalize 는 usage_logs 행을 쓰지 않고 예약만 해제한다(그 경로의
        #    조기 반환). 그래서 무조건 호출이 안전하다.
        try:
            await on_usage(merged)
        except Exception:
            logger.warning("web_search.on_usage_failed")

    if final_status == 200 and isinstance(final_body.get("usage"), dict):
        # Overwrite the returned body's usage with the merged (multi-turn) totals so the
        # client sees the full accounting; reasoning stays a submetric.
        final_body["usage"]["input_tokens"] = merged.input_tokens
        final_body["usage"]["output_tokens"] = merged.output_tokens
    # Strip thinking/redacted_thinking from the CLIENT-returned body: the client replays
    # this (possibly stitched) assistant message on its next turn, and Bedrock rejects a
    # modified thinking block. Prior-turn thinking may be omitted on a new user turn, so
    # this is safe. (Mirrors the streaming path's client-side suppression.)
    if final_status == 200 and isinstance(final_body.get("content"), list):
        # ⚠️ 우리 web_search tool_use 블록도 함께 걷어낸다. 클라이언트 도구 호출과 우리
        #    검색이 **같은 턴**에 함께 오면(두 방언 모두 병렬 도구 호출을 지원한다) 그 턴은
        #    terminal 이라 이 본문이 그대로 나가고, 클라이언트는 자기가 선언하지 않은
        #    ``web_search`` 도구 호출을 받는다. Anthropic 계약상 모든 tool_use 는 tool_result
        #    로 답해야 하므로, 클라이언트는 없는 도구를 실행하려 하거나(Claude Code 는 알 수
        #    없는 도구로 보고한다) 다음 턴에서 400 을 받는다. 스트리밍 경로는 이 블록들을
        #    이미 억제한다 — 비스트리밍만 빠져 있었다.
        final_body["content"] = [
            b for b in final_body["content"]
            if isinstance(b, dict)
            and b.get("type") not in ("thinking", "redacted_thinking")
            and not (b.get("type") == "tool_use" and b.get("name") == GW_WEB_SEARCH_NAME)
        ]
        # 우리 것만 지웠는데 stop_reason 이 tool_use 로 남으면 클라이언트는 보이지 않는
        # 도구 호출을 기다린다 — 스트리밍 스티처의 같은 판단.
        if final_body.get("stop_reason") == "tool_use" and not any(
            _is_client_tool_use_block(b) for b in final_body["content"]
        ):
            final_body["stop_reason"] = "end_turn"
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
    max_result_chars: int = 0,
    max_searches_per_turn: int = 0,
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
    #: ⚠️ 이 둘은 **턴 단위** 상태다. 루프 스코프에 두면 답변 턴이 종료 이벤트 없이 죽었을
    #:    때 직전 검색 턴의 객체와 "response.completed" 가 그대로 최종 프레임으로 나간다 —
    #:    잘린 답변에 붙은 조작된 성공이다. 게다가 그 객체의 output 은 우리 web_search 호출을
    #:    올바르게 제거했기 때문에 **비어 있어서**, 델타가 아니라 최종 객체로 답을 재구성하는
    #:    클라이언트(Codex 계열)는 빈 답변을 받고 완료로 기록한다. 첫 턴에서 죽으면
    #:    final_response_obj 가 None 이라 `id` 조차 없는 completed 가 나간다.
    final_response_obj: Optional[dict] = None
    final_terminal_type = "response.completed"  # actual upstream terminal type (F-1)
    #: 이 턴에서 실제로 종료 이벤트를 받았는지. 못 받았으면 종료를 지어내지 않는다.
    saw_terminal_event = False
    #: 클라이언트가 본 봉투의 id — **첫 턴의** response.created 에서 온 값이다.
    #: ⚠️ 종료 프레임의 id 는 이것과 같아야 한다. 마지막 턴의 id 를 쓰면 클라이언트는
    #:    자기가 열지 않은 응답의 종료를 받는다: created(resp_1) … completed(resp_2).
    #:    id 로 요청을 상관짓는 클라이언트/로그는 그 응답을 찾지 못한다.
    envelope_response_id: str | None = None
    our_call_ids: set[str] = set()  # our web_search call_ids to strip from final output (F-3 Responses)
    error_seen = False       # a 200-stream `error` event occurred (NEW round2 High-1)

    try:
        while True:
            force_final = search_attempts >= max_iterations or time.monotonic() > deadline
            turn_body = _with_web_search_tool(base_body, "responses", include=not force_final)
            turn_body = dict(turn_body)
            # force_final 턴의 배관 제거 — 근거는 anthropic 스티처의 같은 주석 참조.
            turn_body["input"] = (
                _strip_responses_web_search_items(conv_input, our_call_ids)
                if force_final
                else conv_input
            )
            turn_body["stream"] = True
            status, chunk_iter, _h, _rid = await invoke_stream(turn_body)
            if status != 200:
                async for b in _drain_responses_error(chunk_iter, envelope_open):
                    yield b
                return

            # 턴 단위 상태 재설정(위 선언부 주석의 이유).
            saw_terminal_event = False

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
                        envelope_response_id = ((ev.get("response") or {}).get("id"))
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
                    elif _is_client_tool_call_item(item, our_call_ids):
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
                    saw_terminal_event = True
                    final_response_obj = resp_obj
                    final_terminal_type = etype  # preserve incomplete/failed, don't fake completed (F-1)
                    # Responses `input_tokens` INCLUDES both cached_tokens and
                    # cache_write_tokens — parse per turn into exclusive buckets before
                    # accumulating, so merged.input_tokens stays the non-cached billable
                    # input (TokenUsage contract). Per TURN, not on the final sum: each
                    # turn caches a different amount, and typically exactly one turn of a
                    # search loop writes the cache while the rest read it.
                    _merge_usage(merged, extract_responses_usage(resp_obj))
                    # captured; emit our own terminal event at envelope close
                elif etype == "error":
                    error_seen = True  # NEW round2 High-1: do not also emit a fake completed
                    yield _sse("error", ev)
                elif isinstance(ev.get("error"), dict):
                    # ⚠️ 어댑터들이 실제로 내보내는 형태는 type 이 중첩되어 있다:
                    #    {"error": {"type": "provider_error", ...}}. 최상위 "type" 이 없어
                    #    위 분기에 걸리지 않고 조용히 사라졌다 — 근거는 anthropic 스티처의
                    #    같은 분기 주석 참조.
                    error_seen = True
                    yield _sse("error", {"type": "error", "error": ev["error"]})

            is_search_turn = bool(pending_searches) and not client_tool_present and not error_seen
            # An incomplete/failed upstream turn is terminal even if a search was requested —
            # never loop on a truncated/failed response (F-1).
            if (
                not is_search_turn
                or force_final
                or final_terminal_type != "response.completed"
                or error_seen
                # ⚠️ 종료 이벤트를 못 받은 턴(소켓 절단/타임아웃)도 terminal 이다. 이걸
                #    빼면 pending_searches 가 비어 있으니 위 조건으로 우연히 빠져나가는데,
                #    그때 final_* 는 **직전 턴의 값**이라 조작된 성공이 나간다.
                or not saw_terminal_event
            ):
                # 이 턴에서 종료 이벤트를 못 받았다면 직전 턴의 객체를 물려주지 않는다.
                if not saw_terminal_event:
                    # 직전 턴(검색 턴)의 객체는 이 답변과 무관하다 — 그 output 은 우리
                    # web_search 를 제거해 비어 있어서, 최종 객체로 답을 재구성하는
                    # 클라이언트에게 "빈 답변, 완료" 를 준다. 봉투 id 는
                    # _finalize_responses_obj 가 다시 채운다.
                    final_response_obj = {}
                    if error_seen:
                        # ⚠️ 오류로 끝난 것과 그냥 잘린 것은 **다른 신호**여야 한다.
                        #    failed = 상류가 오류를 반환했다, incomplete = 종료 프레임 없이
                        #    끊겼다. 둘을 합치면 운영자가 provider 장애와 네트워크 절단을
                        #    구분할 수 없다.
                        final_terminal_type = "response.failed"
                    else:
                        final_terminal_type = "response.incomplete"
                        yield _sse(
                            "error",
                            {"type": "error",
                             "error": {"type": "incomplete_stream",
                                       "message": "upstream ended without a terminal event"}},
                        )
                # If a mid-stream `error` occurred, the error frame is the terminal signal —
                # do NOT also emit a synthetic response.completed (NEW round2 High-1). Emit
                # response.failed only if we never got a real terminal event.
                # ⚠️ 봉투(response.created)를 한 번도 못 보냈으면 종료 객체도 만들지
                #    않는다. created 없는 response.completed/failed 는 Responses SDK 가
                #    상관지을 대상이 없어 파싱에서 실패한다 — 상류 오류가 게이트웨이 버그로
                #    보인다. 그때 종료 신호는 이미 나간 error 프레임이다.
                if not envelope_open:
                    break
                if error_seen and final_terminal_type == "response.completed":
                    yield _sse("response.failed",
                               {"type": "response.failed",
                                "response": _finalize_responses_obj(
                                    final_response_obj, merged, global_out_index,
                                    "response.failed", our_call_ids,
                                    envelope_id=envelope_response_id)})
                else:
                    yield _sse(
                        final_terminal_type,
                        {"type": final_terminal_type,
                         "response": _finalize_responses_obj(
                             final_response_obj, merged, global_out_index,
                             final_terminal_type, our_call_ids,
                             envelope_id=envelope_response_id)},
                    )
                break

            # Search turn: run ALL requested searches (F-3) → one function_call_output per call_id,
            # in order. search_attempts guards the loop even if every search fails (F-5).
            # Per-search deadline recheck so a fan-out of many searches can't run uncapped
            # past the total deadline (NEW round2 High-2).
            search_attempts += 1
            outputs = []
            # 턴당 검색 개수 상한 — 근거는 anthropic 스티처의 같은 주석 참조.
            allowance = _turn_search_allowance(len(pending_searches), max_searches_per_turn)
            if allowance < len(pending_searches):
                logger.info(
                    "web_search.turn_fanout_capped",
                    requested=len(pending_searches), allowed=allowance,
                )
            for i, ps in enumerate(pending_searches):
                if i >= allowance:
                    outputs.append({"type": "function_call_output", "call_id": ps["call_id"],
                                    "output": json.dumps({
                                        "error": "per-turn web search limit reached; "
                                                 "answer from the results already provided"})})
                    continue
                if time.monotonic() > deadline:
                    outputs.append({"type": "function_call_output", "call_id": ps["call_id"],
                                    "output": json.dumps({"error": "web search deadline exceeded"})})
                    continue
                result_text, ok = await _do_search(
                    mcp_client, ps["input"], default_max_results, max_result_chars
                )
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
    envelope_id: str | None = None,
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
    # ⚠️ 봉투(첫 response.created)의 id 로 고정한다. 마지막 턴의 id 를 그대로 두면
    #    created(resp_1) … completed(resp_2) 가 되어, id 로 상관짓는 클라이언트와 로그가
    #    그 응답을 찾지 못한다. 여러 턴을 하나의 응답으로 합치는 것이 이 스티처의 계약이다.
    if envelope_id:
        obj["id"] = envelope_id
    if our_call_ids and isinstance(obj.get("output"), list):
        obj["output"] = [
            it for it in obj["output"]
            if not (isinstance(it, dict) and it.get("type") == "function_call"
                    and it.get("call_id") in our_call_ids)
        ]
    # WIRE representation, not the billing one: Responses `input_tokens` must be the GRAND
    # TOTAL prompt count (cache reads AND cache writes included), because that is what the
    # OpenAI spec says and what Codex CLI reads to track context. merged.input_tokens is
    # the non-cached billing bucket, so add both cache buckets back on the way out.
    # Emitting the billing value here would produce cached_tokens > input_tokens — an
    # impossible payload. Both sub-counters are echoed for the same reason.
    wire_input = _wire_input(merged)
    obj["usage"] = {
        "input_tokens": wire_input,
        "output_tokens": merged.output_tokens,
        "total_tokens": wire_input + merged.output_tokens,
        "input_tokens_details": {
            "cached_tokens": merged.cache_read_input_tokens,
            "cache_write_tokens": merged.cache_creation_input_tokens,
        },
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
    max_result_chars: int = 0,
    max_searches_per_turn: int = 0,
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
            our_calls = [
                o for o in output
                if isinstance(o, dict)
                and o.get("type") == "function_call"
                and o.get("name") == GW_WEB_SEARCH_NAME
            ]
            client_calls = [o for o in output if _is_client_tool_call_item(o)]

            if force_final or not our_calls or client_calls:
                break

            search_attempts += 1
            new_items = list(output)
            allowance = _turn_search_allowance(len(our_calls), max_searches_per_turn)
            if allowance < len(our_calls):
                logger.info("web_search.turn_fanout_capped",
                            requested=len(our_calls), allowed=allowance)
            for i, call in enumerate(our_calls):
                if i >= allowance:
                    new_items.append({"type": "function_call_output", "call_id": call.get("call_id"),
                                      "output": json.dumps({
                                          "error": "per-turn web search limit reached; "
                                                   "answer from the results already provided"})})
                    continue
                if time.monotonic() > deadline:
                    new_items.append({"type": "function_call_output", "call_id": call.get("call_id"),
                                      "output": json.dumps({"error": "web search deadline exceeded"})})
                    continue
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                result_text, ok = await _do_search(
                    mcp_client, args, default_max_results, max_result_chars
                )
                if ok:
                    searches_done += 1
                new_items.append(
                    {"type": "function_call_output", "call_id": call.get("call_id"), "output": result_text}
                )
            conv_input = conv_input + new_items
    finally:
        merged.web_search_count = searches_done  # fire on any accrued usage even on late failure (F-9)
        merged.total_tokens = merged.input_tokens + merged.output_tokens
        # 토큰 0 에서도 호출하는 이유는 _anthropic_nonstream 의 같은 블록 주석 참조
        # (예약 해제가 이 콜백에만 달려 있다).
        try:
            await on_usage(merged)
        except Exception:
            logger.warning("web_search.on_usage_failed")

    if final_status == 200 and isinstance(final_body.get("usage"), dict):
        # Same wire-vs-billing split as _finalize_responses_obj: the client must see the
        # cache-INCLUSIVE prompt count that the Responses spec defines.
        wire_input = _wire_input(merged)
        final_body["usage"]["input_tokens"] = wire_input
        final_body["usage"]["output_tokens"] = merged.output_tokens
        final_body["usage"]["total_tokens"] = wire_input + merged.output_tokens
    # ⚠️ 우리 web_search function_call 항목을 응답 output 에서 걷어낸다 — 근거는
    #    _anthropic_nonstream 의 같은 블록 주석, 그리고 스트리밍 쪽
    #    _finalize_responses_obj 가 이미 하고 있는 일과 동일하다. 비스트리밍만 빠져 있었다.
    if final_status == 200 and isinstance(final_body.get("output"), list):
        final_body["output"] = [
            it for it in final_body["output"]
            if not (
                isinstance(it, dict)
                and it.get("type") == "function_call"
                and it.get("name") == GW_WEB_SEARCH_NAME
            )
        ]
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
    max_result_chars: int = 0,
    max_searches_per_turn: int = 0,
    handshake_timeout: float = 10.0,
    #: KI-08 역산 훅. Responses 방언은 usage 가 종결 이벤트 안에만 있어서, 패스스루
    #: 경로의 스트림이 그 전에 끊기면 역산 없이는 usage 가 전부 0 이 되고 usage_logs
    #: 행이 아예 만들어지지 않는다(services/streaming.py 의 같은 주석 참조).
    tokenizer_hook: Callable[[str], Awaitable[int | None]] | None = None,
    response_headers: Optional[dict] = None,
    on_stream_complete: Callable[[str, str], Awaitable[None]] | None = None,
    on_nonstream_complete: Callable[[int, bytes], Awaitable[None]] | None = None,
) -> StreamingResponse | JSONResponse:
    """Run the server-side web-search loop and return the client response.

    ``dialect`` is "anthropic" (/v1/messages) or "responses" (/v1/responses). The loop
    ensures the MCP client is initialized (discovers the WebSearch tool) before starting;
    if that fails, it degrades to a plain pass-through of the original request (no tool).

    ``on_stream_complete`` / ``on_nonstream_complete`` are the request/response **body**
    log hooks. Both are ``None`` when body logging is off, and the routers decide that
    before calling — passing a hook makes the streaming paths accumulate the full SSE
    text in memory, so the decision has to be made up front.

    ⚠️ 이 두 훅이 이 함수의 인자로 있는 이유: 라우터들은 ``if is_stream:`` 블록에서
       본문 로깅을 배선하는데, 이 함수는 **그 블록보다 먼저** 리턴한다. 훅 없이 두면
       웹서치를 켠 프로파일의 요청은 라우터의 로깅 코드를 아예 지나지 않아 조용히
       미기록된다. 그리고 그 조합이 하필 최악이다 — 웹서치를 쓰는 것은 Codex(Mantle)이고
       Mantle 은 AWS 쪽 invocation log 에도 남지 않으므로, 본문의 정본이 **어디에도**
       없게 된다. 여섯 개 리턴 경로 전부에 배선돼 있고, 테스트가 그 개수를 센다.
    """
    deadline = time.monotonic() + total_deadline_sec

    def _log_stream(gen):
        """스트리밍 응답을 본문 로깅 래퍼로 감싼다(훅이 없으면 그대로 통과)."""
        if on_stream_complete is None:
            return gen
        from app.services.body_log_records import wrap_stream_for_body_log

        return wrap_stream_for_body_log(
            gen, dialect=dialect, on_complete=on_stream_complete
        )

    async def _log_nonstream(status: int, raw: bytes) -> None:
        """비스트리밍 응답 본문을 기록한다. 실패해도 요청을 깨뜨리지 않는다."""
        if on_nonstream_complete is None:
            return
        try:
            await on_nonstream_complete(status, raw)
        except Exception:
            logger.warning("web_search.body_log_failed")

    # Strip Anthropic/OpenAI NATIVE web_search tool(s) up front: Bedrock/Mantle reject
    # them, and we fulfill the intent via our own loop. Doing it here (before F-7) means
    # a native-only request no longer trips _client_declares_web_search, so the loop runs;
    # a genuine CUSTOM web_search tool (no native type) survives and is still respected.
    initial_req_data = _strip_native_web_search(initial_req_data)

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
                request, chunk_iter, on_usage=_stream_on_usage,
                tokenizer_hook=tokenizer_hook)
            return StreamingResponse(_log_stream(gen), status_code=status,
                                     media_type="text/event-stream", headers=response_headers)
        base.pop("stream", None)
        status, body, _h, usage = await invoke(base)
        if usage and (usage.input_tokens + usage.output_tokens) > 0:
            await on_usage(usage)
        await _log_nonstream(status, body)
        try:
            content = json.loads(body)
        except (ValueError, TypeError):
            content = {"error": {"type": "api_error", "message": "invalid provider response"}}
        return JSONResponse(status_code=status, content=content, headers=response_headers)

    # Ensure the AgentCore WebSearch tool is discoverable before we advertise it to the
    # model. If discovery fails, fall back to a normal (no-search) call so the request
    # still succeeds — the model simply lacks web search this time.
    try:
        # ⚠️ 여기에도 상한이 필요하다. 클라이언트 안쪽에 상한이 있어도, 이 지점은 요청
        #    진입 경로이므로 상한을 두 번 거는 것이 아니라 **최악의 경우를 명시**하는 것이다:
        #    핸드셰이크가 늦어지는 동안 이 요청은 이미 만든 RPM/TPM/CPH 예약을 물고 있다.
        #    asyncio.TimeoutError 는 Exception 하위라 아래 except 가 그대로 잡아
        #    검색 없는 단일 턴으로 폴백한다.
        await asyncio.wait_for(
            mcp_client.ensure_initialized(),
            timeout=handshake_timeout,
        )
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
                gen = bedrock_anthropic_sse_stream(
                    request, chunk_iter, on_usage=_stream_on_usage,
                    tokenizer_hook=tokenizer_hook)
            else:
                gen = responses_sse_stream(
                    request, chunk_iter, on_usage=_stream_on_usage,
                    tokenizer_hook=tokenizer_hook)
            return StreamingResponse(_log_stream(gen), status_code=status,
                                     media_type="text/event-stream", headers=response_headers)
        status, body, _h, usage = await invoke(base)
        if usage and (usage.input_tokens + usage.output_tokens) > 0:
            await on_usage(usage)
        await _log_nonstream(status, body)
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
            max_result_chars=max_result_chars,
            max_searches_per_turn=max_searches_per_turn,
        )
        return StreamingResponse(
            _log_stream(gen), status_code=200,
            media_type="text/event-stream", headers=response_headers,
        )

    loop = _anthropic_nonstream if dialect == "anthropic" else _responses_nonstream
    resp = await loop(
        invoke=invoke,
        base_body=initial_req_data,
        mcp_client=mcp_client,
        on_usage=on_usage,
        max_iterations=max_iterations,
        deadline=deadline,
        default_max_results=default_max_results,
        max_result_chars=max_result_chars,
        max_searches_per_turn=max_searches_per_turn,
    )
    # 루프가 조립해 반환한 최종 본문을 기록한다. 여기서는 `resp.body` 를 읽는다 —
    # 루프 내부가 여러 턴의 결과를 합쳐 만든 것이므로 어떤 단일 턴의 provider 응답도
    # 클라이언트가 실제로 받는 것과 같지 않다. 클라이언트가 받은 바이트가 정본이다.
    await _log_nonstream(resp.status_code, bytes(resp.body or b""))
    return resp
