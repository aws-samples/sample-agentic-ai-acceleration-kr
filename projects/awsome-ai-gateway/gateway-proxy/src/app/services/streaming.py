# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import structlog
from starlette.requests import Request

from app.schemas.domain import TokenUsage

logger = structlog.get_logger(__name__)

# 2번째 인자 = 첫 콘텐츠 델타의 time.monotonic() (미검출 시 None)
OnUsage = Callable[[TokenUsage, float | None], Awaitable[None]] | None
# KI-08 tokenizer fallback: 누적된 output 텍스트를 받아 토큰 수를 역산.
# None 반환 시 추정 불가 (cost_recorder는 0 토큰 + estimated=True로 기록).
TokenizerHook = Callable[[str], Awaitable[int | None]] | None


async def bedrock_anthropic_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float = 60.0,
    drain_timeout: float = 30.0,
    tokenizer_hook: TokenizerHook = None,
) -> AsyncIterator[bytes]:
    """Bedrock EventStream chunks → Anthropic SSE-formatted bytes.

    Each upstream chunk is a JSON blob (one Anthropic event); we emit
    `event: <type>\\ndata: <json>\\n\\n` to the client. Token usage is
    aggregated across `message_start` (input + cache) and `message_delta`
    (output), with `amazon-bedrock-invocationMetrics` as a fallback.

    Edge case handling:
    - Client disconnect: stop yielding; drain remaining chunks in a
      background task so usage is still recorded (best-effort).
    - Idle timeout per chunk (default 60s): emit `event: error` SSE and
      return. Prevents hung upstream streams from pinning the connection.
    - Upstream exception mid-stream: emit `event: error` SSE with the
      error message and return gracefully (do not propagate).
    - Malformed JSON chunk: passthrough as `data: <raw>\\n\\n` (no crash).
    """
    counters = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    }
    accumulated_text: list[str] = []  # KI-08: content_block_delta.delta.text 누적
    first_token_time: float | None = None
    iterator = chunk_iter.__aiter__()
    client_disconnected = False

    def _format(chunk: bytes) -> bytes:
        """Parse chunk, update counters, return SSE-formatted bytes."""
        nonlocal first_token_time
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, TypeError):
            raw = (
                chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            )
            return f"data: {raw}\n\n".encode()

        etype = data.get("type", "unknown")
        if etype == "message_start":
            u = data.get("message", {}).get("usage", {})
            if v := u.get("input_tokens"):
                counters["input_tokens"] = v
            counters["cache_creation"] = u.get(
                "cache_creation_input_tokens", counters["cache_creation"]
            )
            counters["cache_read"] = u.get("cache_read_input_tokens", counters["cache_read"])
        elif etype == "content_block_delta":
            # KI-08: 스트림 도중 생성된 텍스트 누적. disconnect 시 tokenizer 역산용.
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                if t := delta.get("text"):
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    accumulated_text.append(t)
        elif etype == "message_delta":
            u = data.get("usage", {})
            if v := u.get("output_tokens"):
                counters["output_tokens"] = v
        elif m := data.get("amazon-bedrock-invocationMetrics"):
            # invocationMetrics는 Bedrock이 최종 집계한 billable 토큰 수.
            # message_delta.usage.output_tokens와 미세하게 다를 수 있으므로
            # (extended thinking 모델에서 ~3 토큰 차이) 항상 이 값으로 덮어쓴다.
            if v := m.get("inputTokenCount"):
                counters["input_tokens"] = v
            if v := m.get("outputTokenCount"):
                counters["output_tokens"] = v

        return f"event: {etype}\ndata: {json.dumps(data)}\n\n".encode()

    def _current_usage() -> TokenUsage | None:
        it, ot = counters["input_tokens"], counters["output_tokens"]
        if not (it or ot):
            return None
        return TokenUsage(
            input_tokens=it,
            output_tokens=ot,
            total_tokens=it + ot,
            cache_creation_input_tokens=counters["cache_creation"],
            cache_read_input_tokens=counters["cache_read"],
        )

    async def _estimate_if_needed(usage: TokenUsage | None) -> TokenUsage | None:
        """KI-08: output_tokens=0 인데 누적 텍스트가 있으면 tokenizer로 역산.

        disconnect 케이스: message_start로 input_tokens는 있으나 message_delta 전
        끊김 → output_tokens=0. 누적된 content_block_delta text로 역산.
        """
        if not tokenizer_hook or not accumulated_text:
            return usage
        it = counters["input_tokens"]
        ot = counters["output_tokens"]
        if ot > 0:
            return usage  # provider에서 실제 usage 이벤트 수신됨 → 역산 불필요
        try:
            estimated_ot = await tokenizer_hook("".join(accumulated_text))
        except Exception:
            logger.warning("tokenizer_hook_failed")
            estimated_ot = None
        if not estimated_ot or estimated_ot <= 0:
            return usage
        return TokenUsage(
            input_tokens=it,
            output_tokens=estimated_ot,
            total_tokens=it + estimated_ot,
            cache_creation_input_tokens=counters["cache_creation"],
            cache_read_input_tokens=counters["cache_read"],
            estimated=True,
        )

    async def _fire_on_usage() -> None:
        """KI-08: usage 미추출 시에도 빈 TokenUsage로 콜백 실행.

        라우터가 TPM 예약을 설정한 경우, 빈 usage로도 콜백이 돌아가야
        cost_recorder가 ``settle_tpm(actual=0)``을 호출해 예약 해제함.
        누적 텍스트가 있으면 tokenizer로 output_tokens 역산 시도.
        """
        if not on_usage:
            return
        base = _current_usage()
        estimated = await _estimate_if_needed(base)
        usage = estimated or base or TokenUsage(
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _drain_remaining() -> None:
        deadline = time.monotonic() + drain_timeout
        try:
            async for chunk in iterator:
                if time.monotonic() > deadline:
                    logger.warning("stream_drain_timeout")
                    break
                _format(chunk)  # side effect: updates counters
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            await _fire_on_usage()

    # Starlette `is_disconnected()` 는 ASGI 스트리밍 응답 컨텍스트에서 신뢰할
    # 수 없어 (false positive 로 첫 iteration 부터 True 반환) 명시적 체크를
    # 제거. 실제 client 끊김은 아래 except (asyncio.CancelledError, GeneratorExit)
    # 가 잡아낸다.
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("stream_idle_timeout", idle_timeout=idle_timeout)
                err = {
                    "type": "error",
                    "error": {
                        "type": "timeout_error",
                        "message": f"upstream idle timeout after {idle_timeout}s",
                    },
                }
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
                return

            yield _format(chunk)

    except (asyncio.CancelledError, GeneratorExit):
        # Starlette client-disconnect / upstream cancellation. Spawn background
        # drain so usage is still recorded, then re-raise per asyncio contract.
        logger.info("bedrock_stream_cancelled")
        client_disconnected = True
        asyncio.create_task(_drain_remaining())
        raise

    except Exception as exc:
        logger.exception("bedrock_stream_proxy_error")
        err = {
            "type": "error",
            "error": {"type": "stream_error", "message": str(exc) or "stream_error"},
        }
        yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
        return

    if not client_disconnected:
        await _fire_on_usage()


async def openai_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float = 60.0,
    drain_timeout: float = 30.0,
    tokenizer_hook: TokenizerHook = None,
) -> AsyncIterator[bytes]:
    """OpenAI-compatible SSE chunks → passthrough bytes (no re-formatting).

    OpenAI upstreams already emit SSE-formatted frames (`data: {...}\\n\\n`,
    `data: [DONE]\\n\\n`). A single httpx chunk may contain one or many
    frames. We yield chunks as-is and scan each chunk for a `usage` object
    (vLLM emits it on the final chunk when `stream_options.include_usage`).

    Edge case handling mirrors `bedrock_anthropic_sse_stream`:
    - Client disconnect: stop, background-drain so usage is still recorded.
    - Idle timeout per chunk (default 60s): emit an OpenAI-shaped error
      chunk (`data: {"error":{"type":"timeout_error",...}}\\n\\n`) and return.
    - Upstream exception mid-stream: same OpenAI-shaped error chunk path.
    """
    latest_usage: TokenUsage | None = None
    accumulated_text: list[str] = []  # KI-08: delta.content 누적
    first_token_time: float | None = None
    iterator = chunk_iter.__aiter__()
    client_disconnected = False

    def _emit_error_chunk(err_type: str, message: str) -> bytes:
        payload = {"error": {"type": err_type, "message": message}}
        return f"data: {json.dumps(payload)}\n\n".encode()

    def _scan_usage(chunk: bytes) -> TokenUsage | None:
        """Scan a (possibly multi-frame) chunk for usage + accumulate delta content."""
        nonlocal first_token_time
        try:
            text = chunk.decode("utf-8", errors="ignore")
        except Exception:
            return None
        found: TokenUsage | None = None
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            # KI-08: delta content 누적. OpenAI SSE: {"choices":[{"delta":{"content":"..."}}]}
            for choice in data.get("choices", []) or []:
                delta = choice.get("delta") or {}
                if isinstance(delta, dict) and (c := delta.get("content")):
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    accumulated_text.append(c)
            if u := data.get("usage"):
                found = TokenUsage(
                    input_tokens=u.get("prompt_tokens", 0),
                    output_tokens=u.get("completion_tokens", 0),
                    total_tokens=u.get("total_tokens", 0),
                )
        return found

    async def _estimate_if_needed(usage: TokenUsage | None) -> TokenUsage | None:
        """KI-08: usage 없고 누적 텍스트 있으면 tokenizer 역산."""
        if not tokenizer_hook or not accumulated_text:
            return usage
        if usage and usage.output_tokens > 0:
            return usage
        try:
            estimated_ot = await tokenizer_hook("".join(accumulated_text))
        except Exception:
            logger.warning("tokenizer_hook_failed")
            estimated_ot = None
        if not estimated_ot or estimated_ot <= 0:
            return usage
        # OpenAI path에서는 input_tokens가 없음 (usage 이벤트 없으면) — 0으로 둠.
        it = usage.input_tokens if usage else 0
        return TokenUsage(
            input_tokens=it,
            output_tokens=estimated_ot,
            total_tokens=it + estimated_ot,
            estimated=True,
        )

    async def _fire_on_usage() -> None:
        """KI-08: latest_usage 없어도 빈 TokenUsage로 콜백 실행 (TPM 예약 해제용).

        누적 텍스트가 있으면 tokenizer로 output_tokens 역산 시도.
        """
        if not on_usage:
            return
        estimated = await _estimate_if_needed(latest_usage)
        usage = estimated or latest_usage or TokenUsage(
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _drain_remaining() -> None:
        nonlocal latest_usage
        deadline = time.monotonic() + drain_timeout
        try:
            async for chunk in iterator:
                if time.monotonic() > deadline:
                    logger.warning("stream_drain_timeout")
                    break
                if u := _scan_usage(chunk):
                    latest_usage = u
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            await _fire_on_usage()

    # Starlette `is_disconnected()` 는 ASGI 스트리밍 응답 컨텍스트에서 신뢰할
    # 수 없어 (false positive 로 첫 iteration 부터 True 반환) 명시적 체크를
    # 제거. 실제 client 끊김은 아래 except (asyncio.CancelledError, GeneratorExit)
    # 가 잡아낸다.
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("stream_idle_timeout", idle_timeout=idle_timeout)
                yield _emit_error_chunk(
                    "timeout_error", f"upstream idle timeout after {idle_timeout}s"
                )
                return

            if u := _scan_usage(chunk):
                latest_usage = u
            yield chunk

    except (asyncio.CancelledError, GeneratorExit):
        # Starlette client-disconnect / upstream cancellation. See the twin
        # handler in `bedrock_anthropic_sse_stream` for rationale.
        logger.info("openai_stream_cancelled")
        client_disconnected = True
        asyncio.create_task(_drain_remaining())
        raise

    except Exception as exc:
        logger.exception("openai_stream_proxy_error")
        yield _emit_error_chunk("stream_error", str(exc) or "stream_error")
        return

    if not client_disconnected:
        await _fire_on_usage()


async def responses_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float = 60.0,
    drain_timeout: float = 30.0,
) -> AsyncIterator[bytes]:
    """OpenAI **Responses API** → re-framed SSE (`event: {type}\\ndata: {json}\\n\\n`).

    The MantleOpenAIAdapter yields RAW JSON event payloads (one per chunk, `data:`
    prefix already stripped — same contract as MantleAdapter/bedrock_anthropic_sse_stream).
    This stream parses each raw JSON chunk, accumulates text/usage, and re-frames it as
    a proper SSE event for the client (mirrors bedrock_anthropic_sse_stream, NOT the
    openai_sse_stream passthrough which assumes upstream is already SSE-framed).

    Usage shape: the terminal `response.completed` event carries final usage nested at
    `event["response"]["usage"]` (input/output/total + output_tokens_details.reasoning_tokens).
    `response.incomplete`/`response.failed` may carry usage or null. Text deltas arrive
    as `response.output_text.delta`. reasoning_tokens is a submetric (already inside
    output_tokens) — never re-added to total/cost.
    """
    latest_usage: TokenUsage | None = None
    accumulated_text: list[str] = []
    first_token_time: float | None = None
    iterator = chunk_iter.__aiter__()
    client_disconnected = False

    def _emit_error_chunk(err_type: str, message: str) -> bytes:
        payload = {"error": {"type": err_type, "message": message}}
        return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()

    def _usage_from_response(resp: dict) -> TokenUsage | None:
        u = resp.get("usage")
        if not isinstance(u, dict):
            return None
        in_details = u.get("input_tokens_details") or {}
        out_details = u.get("output_tokens_details") or {}
        it = int(u.get("input_tokens", 0) or 0)
        ot = int(u.get("output_tokens", 0) or 0)
        return TokenUsage(
            input_tokens=it,
            output_tokens=ot,
            total_tokens=int(u.get("total_tokens", 0) or 0) or (it + ot),
            cache_read_input_tokens=int(in_details.get("cached_tokens", 0) or 0),
            reasoning_tokens=int(out_details.get("reasoning_tokens", 0) or 0),
        )

    def _process(chunk: bytes) -> bytes:
        """Parse a RAW JSON event chunk → update usage/text, return re-framed SSE bytes.

        Malformed JSON is passed through as a bare `data:` frame (no crash), matching
        bedrock_anthropic_sse_stream's defensive behaviour.
        """
        nonlocal latest_usage, first_token_time
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raw = chunk.decode("utf-8", errors="ignore").strip()
            return f"data: {raw}\n\n".encode()
        if isinstance(data, dict):
            etype = data.get("type", "")
            if etype == "response.output_text.delta":
                if (d := data.get("delta")) and isinstance(d, str):
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    accumulated_text.append(d)
            elif etype in ("response.completed", "response.incomplete", "response.failed"):
                resp = data.get("response")
                if isinstance(resp, dict) and (u := _usage_from_response(resp)):
                    latest_usage = u
            etype_label = etype or "message"
            return f"event: {etype_label}\ndata: {json.dumps(data)}\n\n".encode()
        # Non-dict JSON (unexpected) — re-frame defensively.
        return f"data: {json.dumps(data)}\n\n".encode()

    async def _fire_on_usage() -> None:
        if not on_usage:
            return
        usage = latest_usage or TokenUsage()
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _drain_remaining() -> None:
        deadline = time.monotonic() + drain_timeout
        try:
            async for chunk in iterator:
                if time.monotonic() > deadline:
                    logger.warning("responses_stream_drain_timeout")
                    break
                _process(chunk)  # updates latest_usage/accumulated_text as a side effect
        except Exception:
            logger.exception("responses_stream_drain_error")
        finally:
            await _fire_on_usage()

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("responses_stream_idle_timeout", idle_timeout=idle_timeout)
                yield _emit_error_chunk(
                    "timeout_error", f"upstream idle timeout after {idle_timeout}s"
                )
                return

            yield _process(chunk)

    except (asyncio.CancelledError, GeneratorExit):
        logger.info("responses_stream_cancelled")
        client_disconnected = True
        asyncio.create_task(_drain_remaining())
        raise

    except Exception as exc:
        logger.exception("responses_stream_proxy_error")
        yield _emit_error_chunk("stream_error", str(exc) or "stream_error")
        return

    if not client_disconnected:
        await _fire_on_usage()


async def stream_response(
    request: Request,
    chunk_iterator: AsyncIterator[bytes],
    on_usage: callable,
    idle_timeout: float = 60.0,
    drain_timeout: float = 30.0,
) -> AsyncIterator[bytes]:
    """스트리밍 응답 프록시.

    클라이언트에 chunk를 yield하며, 연결이 끊어지면 백그라운드에서
    스트림을 계속 소비하여 usage를 기록한다.
    """
    usage: TokenUsage | None = None
    client_disconnected = False

    async def consume_remaining():
        """클라이언트 연결 끊김 후 백그라운드 소비."""
        nonlocal usage
        deadline = time.monotonic() + drain_timeout
        try:
            async for chunk in chunk_iterator:
                if time.monotonic() > deadline:
                    logger.warning("stream_drain_timeout")
                    break
                parsed_usage = _try_extract_usage(chunk)
                if parsed_usage:
                    usage = parsed_usage
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            if usage and callable(on_usage):
                try:
                    await on_usage(usage, None)
                except Exception:
                    logger.exception("on_usage_callback_failed")

    try:
        async for chunk in chunk_iterator:
            # 클라이언트 연결 확인
            if await request.is_disconnected():
                logger.info("client_disconnected_during_stream")
                client_disconnected = True
                # 백그라운드에서 나머지 소비
                asyncio.create_task(consume_remaining())
                return

            parsed_usage = _try_extract_usage(chunk)
            if parsed_usage:
                usage = parsed_usage

            yield chunk

    except Exception:
        logger.exception("stream_proxy_error")
        client_disconnected = True

    if not client_disconnected and usage and callable(on_usage):
        try:
            await on_usage(usage, None)
        except Exception:
            logger.exception("on_usage_callback_failed")


def _try_extract_usage(chunk: bytes) -> TokenUsage | None:
    """청크에서 usage 추출 시도 (OpenAI SSE 형식)."""
    try:
        text = chunk.decode("utf-8", errors="ignore")
        for line in text.split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                data = json.loads(line[6:])
                if usage := data.get("usage"):
                    return TokenUsage(
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                        total_tokens=usage.get("total_tokens", 0),
                    )
    except Exception:
        pass
    return None
