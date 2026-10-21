# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""OpenAI-dialect wire facts — the ONE place both Bedrock planes and both wires agree.

Usage parsers, plus the terminal-frame test both streaming adapters need (see the bottom
section). Same reason for living together: two adapters that must not drift.

Four call sites bill from these numbers and they must never disagree:

    providers/mantle_openai_adapter   (Mantle bearer plane,  non-streaming Responses)
    providers/bedrock_openai_adapter  (runtime SigV4 plane,  non-streaming Responses/Chat)
    services/streaming.responses_sse_stream   (both planes, streaming Responses)
    services/streaming.openai_sse_stream      (both planes + vLLM, streaming Chat)

Before this module the Responses parser was copy-pasted between the Mantle adapter and
``responses_sse_stream`` with a comment begging the two to stay identical; they drifted the
moment a third caller appeared. Everything here is pure (dict in, TokenUsage out) so a
single unit test pins streamed == non-streamed == both planes.

WIRE FACTS, measured live 2026-09-03 against ``openai.gpt-5.6-terra`` (Mantle) and
``us.openai.gpt-5.6-terra`` (bedrock-runtime) — the two planes returned byte-identical
usage objects:

    Responses:  usage.input_tokens                            grand total (see below)
                usage.input_tokens_details.cached_tokens      cache READ  (subset)
                usage.input_tokens_details.cache_write_tokens cache WRITE (subset)
                usage.output_tokens                           already includes reasoning
                usage.output_tokens_details.reasoning_tokens  submetric only
                usage.total_tokens                            input_total + output

    Chat:       usage.prompt_tokens                             grand total
                usage.prompt_tokens_details.cached_tokens       cache READ  (subset)
                usage.prompt_tokens_details.cache_write_tokens  cache WRITE (subset)
                usage.completion_tokens / completion_tokens_details.reasoning_tokens
                usage.total_tokens

The prompt count is CACHE-INCLUSIVE on both wires, while ``TokenUsage`` is Anthropic-shaped
(mutually exclusive buckets), so :func:`split_openai_input` must run at parse time — see its
docstring for the measurement and the mis-billing it prevents.
"""
from __future__ import annotations

import json

from app.schemas.domain import TokenUsage, split_openai_input


def _usage_from_parts(
    *,
    prompt_total: int,
    cached: int,
    cache_write: int,
    output: int,
    reasoning: int,
    provider_total: int,
) -> TokenUsage:
    input_tokens, cache_read, cache_creation = split_openai_input(
        prompt_total, cached, cache_write
    )
    total = provider_total or (input_tokens + cache_read + cache_creation + output)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output,
        total_tokens=total,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        reasoning_tokens=reasoning,
    )


def _int(value) -> int:
    """Coerce a provider-reported counter to a non-negative int.

    Providers have been observed sending ``null`` for a counter they do not populate
    (``usage: null`` on non-final Chat chunks, ``cached_tokens: null``), and a bad float
    would break the exclusive-bucket arithmetic. Anything non-numeric becomes 0 rather
    than raising: a usage object that fails to parse must degrade to "no usage recorded",
    never to a 500 on a request the model already answered and charged for.
    """
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def extract_responses_usage(response_body: dict) -> TokenUsage:
    """Parse an OpenAI **Responses** object (or a ``response.completed`` payload's
    ``response``) into TokenUsage. Returns an all-zero TokenUsage when usage is absent."""
    usage = response_body.get("usage") if isinstance(response_body, dict) else None
    if not isinstance(usage, dict):
        return TokenUsage()
    in_details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    return _usage_from_parts(
        prompt_total=_int(usage.get("input_tokens")),
        cached=_int(in_details.get("cached_tokens")),
        cache_write=_int(in_details.get("cache_write_tokens")),
        output=_int(usage.get("output_tokens")),
        reasoning=_int(out_details.get("reasoning_tokens")),
        provider_total=_int(usage.get("total_tokens")),
    )


def extract_chat_usage(usage: dict) -> TokenUsage:
    """Parse an OpenAI **Chat Completions** ``usage`` object into TokenUsage.

    Also serves vLLM (OPENMODEL), which reports only the three top-level counters — the
    details sub-objects are absent, so the split is a no-op and behaviour is unchanged.
    """
    if not isinstance(usage, dict):
        return TokenUsage()
    in_details = usage.get("prompt_tokens_details") or {}
    out_details = usage.get("completion_tokens_details") or {}
    return _usage_from_parts(
        prompt_total=_int(usage.get("prompt_tokens")),
        cached=_int(in_details.get("cached_tokens")),
        cache_write=_int(in_details.get("cache_write_tokens")),
        output=_int(usage.get("completion_tokens")),
        reasoning=_int(out_details.get("reasoning_tokens")),
        provider_total=_int(usage.get("total_tokens")),
    )


# ── terminal frame detection ──────────────────────────────────────────────────
#
# 두 스트리밍 어댑터는 상류 iteration 이 터지면 오류 프레임을 하나 덧붙인다. 문제는
# **언제** 터지느냐다: httpx 는 마지막 청크를 넘긴 **뒤** 연결 정리 단계에서 예외를 던지는
# 일이 잦다(peer 가 깔끔하지 않게 닫는 경우). 그러면 이미 `response.completed` / `[DONE]`
# 을 받은 클라이언트에게 오류 프레임이 뒤따라 붙는다.
#
# 결과가 나쁜 쪽으로만 틀린다: Codex 는 완료된 응답을 실패로 처리하고 재시도한다 — 우리는
# 이미 그 턴의 토큰을 지불했고, 재시도분도 지불한다. 감사 로그에도 성공한 요청이 오류로
# 남는다.
#
# 그래서 종료 프레임을 이미 보냈으면 오류 프레임을 덧붙이지 않는다(로그만 남긴다). #86 의
# "받지 못한 성공을 주장하지 않는다" 의 거울상이다 — 이미 얻은 성공을 실패로 덮지 않는다.
_RESPONSES_TERMINAL_TYPES = frozenset(
    {"response.completed", "response.incomplete", "response.failed"}
)

_DONE_SENTINEL = "[DONE]"


def is_terminal_responses_payload(payload: str) -> bool:
    """Responses 와이어: 이 payload 가 스트림의 종료 신호인가.

    ``[DONE]`` 센티널과 typed terminal 이벤트를 **둘 다** 본다. 상류가 종료 이벤트를 보낸
    직후 ``[DONE]`` 전에 연결이 끊기는 경우가 있고, 그때도 응답 자체는 완결됐다.
    """
    if not isinstance(payload, str):
        return False
    stripped = payload.strip()
    if stripped == _DONE_SENTINEL:
        return True
    try:
        obj = json.loads(stripped)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and obj.get("type") in _RESPONSES_TERMINAL_TYPES


def chat_chunk_is_terminal(chunk: bytes) -> bool:
    """Chat 와이어: 이 원본 청크에 ``data: [DONE]`` 이 들어 있는가.

    chat 은 바이트 그대로 통과시키므로 파싱하지 않는다. 한 청크에 여러 SSE 줄이 들어올 수
    있어서 정확일치가 아니라 포함으로 본다.
    """
    if not isinstance(chunk, (bytes, bytearray)):
        return False
    return b"[DONE]" in chunk

