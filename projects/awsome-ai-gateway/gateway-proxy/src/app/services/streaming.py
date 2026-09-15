# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import structlog
from starlette.requests import Request

from app.config import get_settings
from app.providers.openai_usage import extract_chat_usage, extract_responses_usage
from app.schemas.domain import TokenUsage

logger = structlog.get_logger(__name__)

# 2번째 인자 = 첫 콘텐츠 델타의 time.monotonic() (미검출 시 None)
OnUsage = Callable[[TokenUsage, float | None], Awaitable[None]] | None
# KI-08 tokenizer fallback: 누적된 output 텍스트를 받아 토큰 수를 역산.
# None 반환 시 추정 불가 (cost_recorder는 0 토큰 + estimated=True로 기록).
TokenizerHook = Callable[[str], Awaitable[int | None]] | None

#: 스트림 종료 훅 — ``(누적된 원본 SSE 프레임 전문, status)``.
#: status 는 ``"success"`` 또는 ``"partial"``(disconnect / timeout / 중간 예외).
#:
#: ⚠️ 이 훅이 설정되면 제너레이터가 **모든 프레임을 메모리에 누적한다.** 동시 스트림 수 ×
#:    응답 크기만큼 메모리를 쓴다. 그래서 훅이 None 이면 한 바이트도 쌓지 않는다 —
#:    본문 로깅이 꺼진 상태의 비용이 0 이어야 한다.
OnComplete = Callable[[str, str], Awaitable[None]] | None


#: 펌프가 미리 읽어 둘 수 있는 청크 수. 상류가 프레임 루프보다 빠를 때의 버퍼이자
#: 배압(backpressure) 지점이다 — 큐가 차면 펌프가 상류 읽기를 멈춘다.
#: ⚠️ 무제한으로 두면 느린 클라이언트 하나가 상류 전체를 메모리에 담는다.
_PUMP_READ_AHEAD = 16

#: 상류가 정상 종료했음을 큐로 알리는 표지. ``None`` 을 쓸 수 없다 — 어댑터가 빈
#: 청크를 흘릴 수 있고 그것과 구별되지 않는다.
_PUMP_EOF = object()


class _UpstreamPump:
    """상류 읽기를 **별도 태스크**로 옮겨, 프레임 루프의 취소가 상류를 닫지 않게 한다.

    왜 필요한가
    -----------
    프레임 루프는 거의 모든 시간을 ``await asyncio.wait_for(iterator.__anext__(), ...)``
    에서 파킹된 상태로 보낸다(모델이 생각하는 동안). 클라이언트가 그 시점에 끊으면
    Starlette 이 응답 태스크를 취소하고, ``CancelledError`` 가 **상류 제너레이터 안으로**
    전달되어 그것을 닫는다. 그래서 과금을 지키려고 띄우는 배수(drain) 태스크는 이미
    끝난 제너레이터를 순회하게 되고 **한 청크도 얻지 못한다.**

    실측(python 3.12, 두 모양을 같은 상류로 비교):
      현재 모양  upstream_got CancelledError → upstream_finally → drain_start →
                 drain_end,  배수 청크 **0개**
      펌프 모양  drain_start → drain_chunk c2..c5 → drain_end,  배수 청크 **4개**

    잃는 것이 무엇인지가 중요하다. Anthropic 방언에서 최종 ``output_tokens`` 를 담은
    ``message_delta`` 와 ``amazon-bedrock-invocationMetrics`` 프레임은 스트림 끝에 온다 —
    끊긴 뒤 배수가 비면 output_tokens 가 0 이고 과금이 tokenizer 추정치로 떨어진다.
    Responses 방언은 더 나쁘다: usage 가 종결 이벤트 **안에만** 있어서 usage 전체가 0 이
    되고, cost_recorder 가 usage_logs 행을 아예 만들지 않는다. 그래서
    ``stream_disponnect_drain_timeout`` 설정은 실질적으로 죽은 설정이었다.

    ⚠️ 도착 시각을 **펌프에서** 찍는다. 프레임 루프에서 찍으면 미리 읽어 둔 청크가
       실제 도착보다 늦은 시각을 받아 TTFT 가 부풀려진다(read-ahead 만큼).
    """

    def __init__(
        self,
        chunk_iter: AsyncIterator[bytes],
        *,
        read_ahead: int = _PUMP_READ_AHEAD,
        label: str = "stream",
    ) -> None:
        self._iterator = chunk_iter.__aiter__()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=read_ahead)
        self._label = label
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """상류를 끝까지 읽어 큐에 넣는다. 예외도 큐로 전달한다.

        ⚠️ 예외를 던지지 않고 큐로 넘기는 이유: 이 태스크는 아무도 await 하지 않으므로,
           여기서 던지면 "Task exception was never retrieved" 로 로그만 남고 프레임
           루프는 영원히 기다린다.
        """
        try:
            async for chunk in self._iterator:
                await self._queue.put((chunk, time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 프레임 루프가 되던진다
            await self._queue.put(exc)
        else:
            await self._queue.put(_PUMP_EOF)

    async def next(self, *, timeout: float) -> tuple[bytes, float]:
        """다음 ``(청크, 도착시각)``. EOF 면 ``StopAsyncIteration``.

        상류 예외는 여기서 그대로 되던진다 — 프레임 루프의 기존 except 절이 잡는다.
        """
        item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if item is _PUMP_EOF:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def drain(self, *, timeout: float) -> AsyncIterator[bytes]:
        """클라이언트가 끊긴 뒤 남은 청크를 계속 받는다(과금 보존).

        ⚠️ 펌프 태스크는 여전히 살아 있으므로 상류는 계속 읽힌다 — 그것이 이 클래스의
           존재 이유다. 여기서 하는 일은 큐를 비우는 것뿐이다.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("stream_drain_timeout", label=self._label)
                return
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except TimeoutError:
                logger.warning("stream_drain_timeout", label=self._label)
                return
            if item is _PUMP_EOF:
                return
            if isinstance(item, BaseException):
                logger.info("stream_drain_upstream_error", label=self._label)
                return
            yield item[0]

    def close(self) -> None:
        """펌프 태스크를 정리한다. 정상 종료·오류 반환 경로에서 부른다.

        ⚠️ ``await`` 하지 않는다 — 이 메서드는 제너레이터가 닫히는 경로에서도 불릴 수
           있고, 그 시점의 await 는 "async generator ignored GeneratorExit" 를 유발한다.
        """
        if not self._task.done():
            self._task.cancel()


def _resolve_timeouts(
    idle_timeout: float | None, drain_timeout: float | None
) -> tuple[float, float]:
    """SSE 타임아웃의 **단일 진실원**: 인자 미지정(None) 시 Settings 에서 해석.

    과거엔 각 헬퍼의 기본값이 하드코딩 60.0/30.0 이었고 **모든 호출부가 인자를 넘기지
    않아** `settings.stream_idle_timeout` / `stream_disconnect_drain_timeout` 이 완전한
    **죽은 설정**이었다 — 차트/env 로 어떤 값을 주입해도 런타임은 60s 로 동작했고,
    그래서 Opus extended thinking 요청이 정상 생성 중에 끊겼다.

    호출부는 5곳(routers/messages.py, routers/openai_compat.py×2,
    services/web_search_loop.py×3 — pass-through 경로 포함)이라 "호출부마다 인자 추가"
    방식은 새 호출부가 하나 생기는 순간 같은 회귀가 재발한다. 그래서 기본값 자체를
    설정에서 끌어오게 만들어 구조적으로 막는다.

    인자를 명시하면 그대로 우선한다(단위테스트가 idle_timeout=0.2 로 타임아웃 경로를
    강제하는 것처럼). 둘 다 명시된 경우엔 get_settings() 를 아예 호출하지 않는다.
    """
    if idle_timeout is not None and drain_timeout is not None:
        return idle_timeout, drain_timeout
    s = get_settings()
    return (
        float(s.stream_idle_timeout) if idle_timeout is None else idle_timeout,
        float(s.stream_disconnect_drain_timeout) if drain_timeout is None else drain_timeout,
    )


async def bedrock_anthropic_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
    tokenizer_hook: TokenizerHook = None,
    on_complete: OnComplete = None,
) -> AsyncIterator[bytes]:
    """Bedrock EventStream chunks → Anthropic SSE-formatted bytes.

    Each upstream chunk is a JSON blob (one Anthropic event); we emit
    `event: <type>\\ndata: <json>\\n\\n` to the client. Token usage is
    aggregated across `message_start` (input + cache) and `message_delta`
    (output), with `amazon-bedrock-invocationMetrics` as a fallback.

    Edge case handling:
    - Client disconnect: stop yielding; drain remaining chunks in a
      background task so usage is still recorded (best-effort).
    - Idle timeout per chunk (기본 = settings.stream_idle_timeout): emit
      `event: error` SSE and return. Prevents hung upstream streams from
      pinning the connection.
    - Upstream exception mid-stream: emit `event: error` SSE with the
      error message and return gracefully (do not propagate).
    - Malformed JSON chunk: passthrough as `data: <raw>\\n\\n` (no crash).
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    counters = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    }
    accumulated_text: list[str] = []  # KI-08: content_block_delta.delta.text 누적
    first_token_time: float | None = None
    pump = _UpstreamPump(chunk_iter, label="anthropic")
    #: 방금 펌프에서 꺼낸 청크의 **도착** 시각. TTFT 를 이 값으로 찍는다.
    #: ⚠️ 포맷 시점의 monotonic() 을 쓰면 미리 읽어 둔 청크(read-ahead)가 실제 도착보다
    #:    늦은 시각을 받아 TTFT 가 부풀려진다.
    chunk_arrived_at: float | None = None
    client_disconnected = False
    # 과금 멱등 가드. 종료 경로가 4개(정상/idle timeout/upstream 예외/클라이언트 끊김
    # → 백그라운드 drain)라, 가드 없이 각 경로에서 finalize 를 호출하면 **이중 과금**이
    # 된다. 반대로 timeout/예외 경로에서 호출을 빼면 그때까지 생성된 토큰이 **유실**된다
    # (과거 동작: 두 경로가 함수 말미의 _fire_on_usage 를 건너뛰는 return 이었다).
    usage_fired = False
    # 본문 로깅용 원본 SSE 프레임 누적. on_complete 가 없으면 아무것도 쌓지 않는다.
    accumulated_frames: list[str] = []
    complete_fired = False

    def _format(chunk: bytes) -> bytes:
        """Parse chunk, update counters, return SSE-formatted bytes."""
        nonlocal first_token_time, chunk_arrived_at
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
                        # 도착 시각(펌프에서 찍음). 포맷 시점이 아니다 — 위 주석 참조.
                        first_token_time = (
                            chunk_arrived_at
                            if chunk_arrived_at is not None
                            else time.monotonic()
                        )
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

    async def _fire_on_complete(status: str) -> None:
        """스트림 종료를 **정확히 1회** 알린다(complete_fired 가드).

        ⚠️ 예외를 밖으로 내지 않는다. 이 훅의 소비자는 본문 로깅(감사)이고, 감사 실패로
           사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다.
        """
        nonlocal complete_fired
        if on_complete is None or complete_fired:
            return
        complete_fired = True
        try:
            await on_complete("".join(accumulated_frames), status)
        except Exception:
            logger.exception("on_complete_callback_failed")

    async def _fire_on_usage() -> None:
        """KI-08: usage 미추출 시에도 빈 TokenUsage로 콜백 실행.

        라우터가 TPM 예약을 설정한 경우, 빈 usage로도 콜백이 돌아가야
        cost_recorder가 ``settle_tpm(actual=0)``을 호출해 예약 해제함.
        누적 텍스트가 있으면 tokenizer로 output_tokens 역산 시도.

        **정확히 1회만** 실행된다(usage_fired 가드) — 위 4개 종료 경로 중
        어디로 빠져도 과금이 유실되지도, 중복되지도 않게.
        """
        nonlocal usage_fired
        if not on_usage or usage_fired:
            return
        usage_fired = True
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
        """클라이언트가 끊긴 뒤에도 남은 프레임을 소비해 과금을 보존한다.

        ⚠️ 펌프를 통해 받는다. 예전에는 ``iterator`` 를 직접 순회했는데, 클라이언트 끊김이
           그 iterator 를 이미 닫아 놓기 때문에 **한 청크도 얻지 못했다**(실측). 즉
           ``stream_disconnect_drain_timeout`` 은 죽은 설정이었다.
        """
        try:
            async for chunk in pump.drain(timeout=drain_timeout):
                _format(chunk)  # side effect: updates counters
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            pump.close()
            await _fire_on_usage()
            await _fire_on_complete("partial")

    # Starlette `is_disconnected()` 는 ASGI 스트리밍 응답 컨텍스트에서 신뢰할
    # 수 없어 (false positive 로 첫 iteration 부터 True 반환) 명시적 체크를
    # 제거. 실제 client 끊김은 아래 except (asyncio.CancelledError, GeneratorExit)
    # 가 잡아낸다.
    try:
        while True:
            try:
                chunk, chunk_arrived_at = await pump.next(timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("stream_idle_timeout", idle_timeout=idle_timeout)
                # ⚠️ yield 보다 **먼저** 확정한다. 클라이언트가 이미 끊긴 상태면 아래
                # yield 가 GeneratorExit 을 던져 except 절로 빠지므로, 뒤에 두면
                # 이 경로의 과금이 다시 유실된다.
                await _fire_on_usage()
                await _fire_on_complete("partial")
                err = {
                    "type": "error",
                    "error": {
                        "type": "timeout_error",
                        "message": f"upstream idle timeout after {idle_timeout}s",
                    },
                }
                yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
                return

            _frame = _format(chunk)
            if on_complete is not None:
                accumulated_frames.append(_frame.decode("utf-8", errors="replace"))
            yield _frame

    except (asyncio.CancelledError, GeneratorExit):
        # Starlette client-disconnect / upstream cancellation. Spawn background
        # drain so usage is still recorded, then re-raise per asyncio contract.
        logger.info("bedrock_stream_cancelled")
        client_disconnected = True
        asyncio.create_task(_drain_remaining())
        raise

    except Exception as exc:
        logger.exception("bedrock_stream_proxy_error")
        # timeout 경로와 동일 이유로 yield 앞에서 확정.
        await _fire_on_usage()
        await _fire_on_complete("partial")
        err = {
            "type": "error",
            "error": {"type": "stream_error", "message": str(exc) or "stream_error"},
        }
        yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
        return

    # 정상 종료 — 펌프 태스크를 정리한다(끊김 경로는 배수의 finally 가 닫는다).
    pump.close()
    if not client_disconnected:
        await _fire_on_usage()
        # 정상 종료 — 클라이언트가 끊기지 않았고 스트림이 끝까지 갔다.
        await _fire_on_complete("success")


async def openai_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
    tokenizer_hook: TokenizerHook = None,
    on_complete: OnComplete = None,
) -> AsyncIterator[bytes]:
    """OpenAI-compatible SSE chunks → passthrough bytes (no re-formatting).

    OpenAI upstreams already emit SSE-formatted frames (`data: {...}\\n\\n`,
    `data: [DONE]\\n\\n`). A single httpx chunk may contain one or many
    frames. We yield chunks as-is and scan each chunk for a `usage` object
    (vLLM emits it on the final chunk when `stream_options.include_usage`).

    Edge case handling mirrors `bedrock_anthropic_sse_stream`:
    - Client disconnect: stop, background-drain so usage is still recorded.
    - Idle timeout per chunk (기본 = settings.stream_idle_timeout): emit an
      OpenAI-shaped error chunk (`data: {"error":{"type":"timeout_error",...}}\\n\\n`)
      and return.
    - Upstream exception mid-stream: same OpenAI-shaped error chunk path.
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    latest_usage: TokenUsage | None = None
    accumulated_text: list[str] = []  # KI-08: delta.content 누적
    first_token_time: float | None = None
    pump = _UpstreamPump(chunk_iter, label="openai")
    #: 방금 펌프에서 꺼낸 청크의 **도착** 시각. TTFT 를 이 값으로 찍는다.
    #: ⚠️ 포맷 시점의 monotonic() 을 쓰면 미리 읽어 둔 청크(read-ahead)가 실제 도착보다
    #:    늦은 시각을 받아 TTFT 가 부풀려진다.
    chunk_arrived_at: float | None = None
    client_disconnected = False
    usage_fired = False  # 과금 멱등 가드 — bedrock_anthropic_sse_stream 과 동일 계약
    accumulated_frames: list[str] = []  # 본문 로깅용(on_complete 없으면 미사용)
    complete_fired = False

    def _emit_error_chunk(err_type: str, message: str) -> bytes:
        payload = {"error": {"type": err_type, "message": message}}
        return f"data: {json.dumps(payload)}\n\n".encode()

    def _scan_usage(chunk: bytes) -> TokenUsage | None:
        """Scan a (possibly multi-frame) chunk for usage + accumulate delta content."""
        nonlocal first_token_time, chunk_arrived_at
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
                        # 도착 시각(펌프에서 찍음). 포맷 시점이 아니다 — 위 주석 참조.
                        first_token_time = (
                            chunk_arrived_at
                            if chunk_arrived_at is not None
                            else time.monotonic()
                        )
                    accumulated_text.append(c)
            if u := data.get("usage"):
                # Shared Chat-wire parser: splits the cache-inclusive prompt count into
                # exclusive TokenUsage buckets and picks up reasoning_tokens. vLLM sends
                # none of the details sub-objects, so its behaviour is byte-identical to
                # the previous three-field construction; GPT-5.6 on either Bedrock plane
                # sends them and would otherwise be billed as if nothing were cached.
                found = extract_chat_usage(u)
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

    async def _fire_on_complete(status: str) -> None:
        """스트림 종료를 **정확히 1회** 알린다(complete_fired 가드).

        ⚠️ 예외를 밖으로 내지 않는다. 이 훅의 소비자는 본문 로깅(감사)이고, 감사 실패로
           사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다.
        """
        nonlocal complete_fired
        if on_complete is None or complete_fired:
            return
        complete_fired = True
        try:
            await on_complete("".join(accumulated_frames), status)
        except Exception:
            logger.exception("on_complete_callback_failed")

    async def _fire_on_usage() -> None:
        """KI-08: latest_usage 없어도 빈 TokenUsage로 콜백 실행 (TPM 예약 해제용).

        누적 텍스트가 있으면 tokenizer로 output_tokens 역산 시도.
        **정확히 1회만** 실행된다(usage_fired 가드).
        """
        nonlocal usage_fired
        if not on_usage or usage_fired:
            return
        usage_fired = True
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
        """근거는 anthropic 헬퍼의 같은 함수 주석 참조(펌프 없이는 0청크)."""
        nonlocal latest_usage
        try:
            async for chunk in pump.drain(timeout=drain_timeout):
                if u := _scan_usage(chunk):
                    latest_usage = u
        except Exception:
            logger.exception("stream_drain_error")
        finally:
            pump.close()
            await _fire_on_usage()
            await _fire_on_complete("partial")

    # Starlette `is_disconnected()` 는 ASGI 스트리밍 응답 컨텍스트에서 신뢰할
    # 수 없어 (false positive 로 첫 iteration 부터 True 반환) 명시적 체크를
    # 제거. 실제 client 끊김은 아래 except (asyncio.CancelledError, GeneratorExit)
    # 가 잡아낸다.
    try:
        while True:
            try:
                chunk, chunk_arrived_at = await pump.next(timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("stream_idle_timeout", idle_timeout=idle_timeout)
                # yield 보다 먼저 확정 (클라이언트가 이미 끊겼으면 yield 가 GeneratorExit).
                await _fire_on_usage()
                await _fire_on_complete("partial")
                yield _emit_error_chunk(
                    "timeout_error", f"upstream idle timeout after {idle_timeout}s"
                )
                return

            if u := _scan_usage(chunk):
                latest_usage = u
            if on_complete is not None:
                accumulated_frames.append(
                    chunk.decode("utf-8", errors="replace")
                    if isinstance(chunk, bytes)
                    else str(chunk)
                )
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
        await _fire_on_usage()  # yield 앞에서 확정 (timeout 경로와 동일 이유)
        await _fire_on_complete("partial")
        yield _emit_error_chunk("stream_error", str(exc) or "stream_error")
        return

    # 정상 종료 — 펌프 태스크를 정리한다(끊김 경로는 배수의 finally 가 닫는다).
    pump.close()
    if not client_disconnected:
        await _fire_on_usage()
        # 정상 종료 — 클라이언트가 끊기지 않았고 스트림이 끝까지 갔다.
        await _fire_on_complete("success")


async def responses_sse_stream(
    request: Request,
    chunk_iter: AsyncIterator[bytes],
    on_usage: OnUsage = None,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
    on_complete: OnComplete = None,
    tokenizer_hook: TokenizerHook = None,
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

    idle/drain 타임아웃 기본값은 Settings 에서 해석된다(_resolve_timeouts).
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
    latest_usage: TokenUsage | None = None
    accumulated_text: list[str] = []
    first_token_time: float | None = None
    pump = _UpstreamPump(chunk_iter, label="responses")
    #: 방금 펌프에서 꺼낸 청크의 **도착** 시각. TTFT 를 이 값으로 찍는다.
    #: ⚠️ 포맷 시점의 monotonic() 을 쓰면 미리 읽어 둔 청크(read-ahead)가 실제 도착보다
    #:    늦은 시각을 받아 TTFT 가 부풀려진다.
    chunk_arrived_at: float | None = None
    client_disconnected = False
    usage_fired = False  # 과금 멱등 가드 — bedrock_anthropic_sse_stream 과 동일 계약
    accumulated_frames: list[str] = []  # 본문 로깅용(on_complete 없으면 미사용)
    complete_fired = False

    def _emit_error_chunk(err_type: str, message: str) -> bytes:
        payload = {"error": {"type": err_type, "message": message}}
        return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()

    def _usage_from_response(resp: dict) -> TokenUsage | None:
        # Same parser as the non-streaming adapters (providers/openai_usage) — that is
        # what guarantees a prompt bills identically streamed vs non-streamed, on both the
        # Mantle and the bedrock-runtime plane. None (not a zero TokenUsage) when the
        # event carries no usage object, so a usage-less `response.incomplete` cannot
        # erase a good reading from an earlier terminal event.
        if not isinstance(resp.get("usage"), dict):
            return None
        return extract_responses_usage(resp)

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
                        # 도착 시각(펌프에서 찍음). 포맷 시점이 아니다 — 위 주석 참조.
                        first_token_time = (
                            chunk_arrived_at
                            if chunk_arrived_at is not None
                            else time.monotonic()
                        )
                    accumulated_text.append(d)
            elif etype in ("response.completed", "response.incomplete", "response.failed"):
                resp = data.get("response")
                if isinstance(resp, dict) and (u := _usage_from_response(resp)):
                    latest_usage = u
            etype_label = etype or "message"
            return f"event: {etype_label}\ndata: {json.dumps(data)}\n\n".encode()
        # Non-dict JSON (unexpected) — re-frame defensively.
        return f"data: {json.dumps(data)}\n\n".encode()

    async def _fire_on_complete(status: str) -> None:
        """스트림 종료를 **정확히 1회** 알린다(complete_fired 가드).

        ⚠️ 예외를 밖으로 내지 않는다. 이 훅의 소비자는 본문 로깅(감사)이고, 감사 실패로
           사용자의 스트림을 깨뜨리는 것은 거래가 성립하지 않는다.
        """
        nonlocal complete_fired
        if on_complete is None or complete_fired:
            return
        complete_fired = True
        try:
            await on_complete("".join(accumulated_frames), status)
        except Exception:
            logger.exception("on_complete_callback_failed")

    async def _estimate_output_tokens() -> TokenUsage | None:
        """KI-08 역산 — 이 방언에만 없던 것.

        ⚠️ 왜 필요한가. Responses 방언은 usage 를 **종결 이벤트(response.completed)
           안에서만** 준다. 그래서 상류가 idle timeout 이나 오류로 그 이벤트 전에 끊기면
           ``latest_usage`` 가 None 이고, ``TokenUsage()``(전부 0)로 발화한다.
           ``cost_recorder.finalize`` 는 total/input/output 이 모두 0 이면 TPM 예약만
           돌려주고 **usage_logs 행을 아예 만들지 않은 채** 리턴한다 — provider 가 이미
           AWS 에 청구한 토큰이 우리 쪽에는 존재하지 않게 된다.

           같은 요청 형태가 ``/v1/messages`` 와 ``/v1/chat/completions`` 에서는 역산되어
           기록된다. 즉 이 누락은 **방언별**이라 집계 대시보드에서는 보이지 않는다.

        ⚠️ input 과 cache 버킷을 **앞으로 이어 나른다.** response.incomplete/failed 는
           input+cache 를 담고 output 만 0 인 경우가 있어서, output 만으로 TokenUsage 를
           새로 만들면 캐시 버킷이 지워져 지금보다 더 과소청구가 된다.
        """
        if not tokenizer_hook or not accumulated_text:
            return None
        base = latest_usage or TokenUsage()
        if base.output_tokens > 0:
            return None  # 실제 usage 를 받았다 — 역산 불필요
        try:
            estimated = await tokenizer_hook("".join(accumulated_text))
        except Exception:
            logger.warning("tokenizer_hook_failed")
            return None
        if not estimated or estimated <= 0:
            return None
        # ⚠️ 새로 만들지 않고 **복사 후 덮어쓴다.** TokenUsage 에는 output 과 무관한 필드가
        #    더 있다(web_search_count — 웹서치 귀속/과금, cache_ttl_1h — 캐시 단가 분기).
        #    필드를 열거해 새로 만들면 나중에 필드가 추가될 때 조용히 유실된다.
        return base.model_copy(
            update={
                "output_tokens": estimated,
                "total_tokens": base.input_tokens + estimated,
                "estimated": True,
            }
        )

    async def _fire_on_usage() -> None:
        """**정확히 1회만** 실행된다(usage_fired 가드)."""
        nonlocal usage_fired
        if not on_usage or usage_fired:
            return
        usage_fired = True
        usage = latest_usage or TokenUsage()
        estimated_usage = await _estimate_output_tokens()
        if estimated_usage is not None:
            usage = estimated_usage
        try:
            await on_usage(usage, first_token_time)
        except Exception:
            logger.exception("on_usage_callback_failed")

    async def _drain_remaining() -> None:
        """근거는 anthropic 헬퍼의 같은 함수 주석 참조.

        ⚠️ 이 방언에서 가장 비싸다: usage 가 종결 이벤트 **안에만** 있어서, 배수가 비면
           usage 전체가 0 이고 cost_recorder 가 usage_logs 행을 아예 만들지 않는다.
        """
        try:
            async for chunk in pump.drain(timeout=drain_timeout):
                _process(chunk)  # updates latest_usage/accumulated_text as a side effect
        except Exception:
            logger.exception("responses_stream_drain_error")
        finally:
            pump.close()
            await _fire_on_usage()
            await _fire_on_complete("partial")

    try:
        while True:
            try:
                chunk, chunk_arrived_at = await pump.next(timeout=idle_timeout)
            except StopAsyncIteration:
                break
            except TimeoutError:
                logger.warning("responses_stream_idle_timeout", idle_timeout=idle_timeout)
                # yield 보다 먼저 확정 (클라이언트가 이미 끊겼으면 yield 가 GeneratorExit).
                await _fire_on_usage()
                await _fire_on_complete("partial")
                yield _emit_error_chunk(
                    "timeout_error", f"upstream idle timeout after {idle_timeout}s"
                )
                return

            _frame = _process(chunk)
            if on_complete is not None:
                accumulated_frames.append(
                    _frame.decode("utf-8", errors="replace")
                    if isinstance(_frame, bytes)
                    else str(_frame)
                )
            yield _frame

    except (asyncio.CancelledError, GeneratorExit):
        logger.info("responses_stream_cancelled")
        client_disconnected = True
        asyncio.create_task(_drain_remaining())
        raise

    except Exception as exc:
        logger.exception("responses_stream_proxy_error")
        await _fire_on_usage()  # yield 앞에서 확정 (timeout 경로와 동일 이유)
        await _fire_on_complete("partial")
        yield _emit_error_chunk("stream_error", str(exc) or "stream_error")
        return

    # 정상 종료 — 펌프 태스크를 정리한다(끊김 경로는 배수의 finally 가 닫는다).
    pump.close()
    if not client_disconnected:
        await _fire_on_usage()
        # 정상 종료 — 클라이언트가 끊기지 않았고 스트림이 끝까지 갔다.
        await _fire_on_complete("success")


async def stream_response(
    request: Request,
    chunk_iterator: AsyncIterator[bytes],
    on_usage: callable,
    idle_timeout: float | None = None,
    drain_timeout: float | None = None,
) -> AsyncIterator[bytes]:
    """스트리밍 응답 프록시.

    클라이언트에 chunk를 yield하며, 연결이 끊어지면 백그라운드에서
    스트림을 계속 소비하여 usage를 기록한다.

    ⚠️ 현재 **호출부 없음**(dialect 별 전용 헬퍼가 대체). 그래도 타임아웃 기본값을
    Settings 에서 해석하도록 맞춰 둔다 — 나중에 누가 이 함수를 쓰기 시작할 때
    하드코딩 60s 로 되돌아가는 회귀를 원천 차단하기 위함.
    """
    idle_timeout, drain_timeout = _resolve_timeouts(idle_timeout, drain_timeout)
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
