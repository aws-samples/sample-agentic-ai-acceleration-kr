# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Bedrock **runtime** plane adapter for the OpenAI wires (SigV4, CRIS model ids).

Same dialect as :class:`~app.providers.mantle_openai_adapter.MantleOpenAIAdapter`, different
plane:

                        Mantle plane                     runtime plane (this file)
    host                bedrock-mantle.{r}.api.aws       bedrock-runtime.{r}.amazonaws.com
    auth                Bearer (BedrockTokenGenerator)   SigV4 (service "bedrock")
    model id            openai.gpt-5.6-terra             us./global.openai.gpt-5.6-terra (CRIS)
    invocation logging  not captured                     captured (streaming INCLUDED)
    request/response    OpenAI Responses / Chat          IDENTICAL

Verified live 2026-09-03 (us-east-2, account 123456789012): both ``/openai/v1/responses``
and ``/openai/v1/chat/completions`` answer 200 non-streaming and emit
``text/event-stream`` with ``stream: true``, and the Responses SSE event taxonomy
(``response.created`` … ``response.output_text.delta`` … ``response.completed`` … ``[DONE]``)
is the same one ``services/streaming.responses_sse_stream`` already parses. That is why
this adapter reuses the existing SSE contract verbatim: yield the raw JSON payload of each
``data:`` line, ``data:`` prefix stripped, ``[DONE]`` dropped.

## Model-invocation logging — measured, not assumed

Measured live 2026-09-03 (123456789012 / us-east-2) by enabling logging to a throwaway
CloudWatch log group, calling, reading the records back, and then deleting the config to
restore the account. Every call carried a unique marker string in its prompt so records
could be attributed by CONTENT, not only by joining on an id:

    plane    wire       stream   records   requestId joins x-amzn-requestid
    runtime  responses  no          1              yes
    runtime  responses  YES         1              yes
    runtime  chat       YES         1              yes
    Mantle   responses  no          0               -
    Mantle   responses  YES         0               -

STREAMING IS CAPTURED — exactly one record per invocation either way, carrying the full
request and response body, ``operation`` = ``Responses``/``ChatCompletions``, and
``modelId`` = the resolved inference-profile ARN. That is why ``invoke_stream`` surfaces
the request id and not just ``invoke``: streaming is the bulk of real traffic, so dropping
its id would leave most of it unauditable even though AWS logs it.

Mantle produced ZERO records on both wires. It does return an ``x-amzn-requestid``, but
the value is an OpenAI-style ``req_...`` string rather than an AWS request id, and no log
record bears it — which is why :class:`MantleOpenAIAdapter` returns ``None``/``{}`` rather
than passing it on. Persisting that id would be worse than NULL: a reconciliation would
treat the row as joinable and then report it as a permanently missing record.

The runtime plane needs a CROSS-REGION INFERENCE PROFILE id: ``openai.gpt-5.6-*`` reports
``inferenceTypesSupported = [INFERENCE_PROFILE]`` and only the ``us.``/``global.`` prefixed
forms are callable. The alias's ``provider_model_id`` therefore carries the prefix, and
picking the right one is an operator/catalogue decision (``us.`` for US regions,
``global.`` where that is the only profile — e.g. ap-northeast-2), not something this
adapter rewrites.
"""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Optional, Protocol

import httpx
import structlog

from app.providers.base import ProviderAdapter
from app.providers.openai_usage import (
    chat_chunk_is_terminal,
    extract_chat_usage,
    extract_responses_usage,
    is_terminal_responses_payload,
)
from app.schemas.domain import TokenUsage

logger = structlog.get_logger(__name__)

# bedrock-runtime.us-east-2.amazonaws.com  ->  us-east-2
_HOST_REGION_RE = re.compile(r"bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com", re.IGNORECASE)
# A callable CRIS id starts with a geo scope. Kept as data, not a hard gate — AWS adds
# scopes (eu./apac./us-gov.) without asking us, and the service's own error message is
# clearer than anything we could invent.
_CRIS_PREFIXES = ("us.", "global.", "eu.", "apac.", "us-gov.")

_WIRE_PATHS = {
    "responses": "/v1/responses",
    "chat": "/v1/chat/completions",
}


class _Signer(Protocol):
    async def sign(
        self,
        *,
        method: str,
        url: str,
        body: bytes,
        region: str,
        headers: Optional[dict] = ...,
        role_arn: Optional[str] = ...,
        external_id: Optional[str] = ...,
    ) -> dict: ...


def _region_for(endpoint: str, profile) -> str:
    """Signing region — taken from the ENDPOINT HOST, with the profile as fallback.

    The host is authoritative because SigV4 binds the signature to the region it names:
    signing ``bedrock-runtime.us-east-2.amazonaws.com`` with ``us-east-1`` is a 403, not a
    cross-region call. A routing profile whose region disagrees with the alias endpoint is
    a misconfiguration, so it is logged rather than silently honoured.
    """
    host_region = None
    if match := _HOST_REGION_RE.search(endpoint or ""):
        host_region = match.group(1)
    profile_region = getattr(profile, "region", None)
    if host_region and profile_region and host_region != profile_region:
        logger.warning(
            "bedrock_openai_region_mismatch",
            endpoint_region=host_region,
            profile_region=profile_region,
            used=host_region,
        )
    region = host_region or profile_region
    if not region:
        raise ValueError(
            f"Cannot determine a signing region for endpoint {endpoint!r}; "
            "expected https://bedrock-runtime.{region}.amazonaws.com/openai"
        )
    return region


def _error_chunk(wire: str, message: str) -> bytes:
    """One error frame, in whatever framing the wire's downstream consumer expects.

    ``responses`` chunks are re-framed by ``responses_sse_stream`` (bare JSON in), while
    ``chat`` chunks are passed through to the client untouched — an unframed payload on
    the chat wire would land in the client's event stream as garbage.
    """
    payload = json.dumps({"error": {"type": "provider_error", "message": message}})
    return payload.encode() if wire == "responses" else f"data: {payload}\n\n".encode()


class BedrockOpenAIAdapter(ProviderAdapter):
    """OpenAI Responses/Chat over ``bedrock-runtime`` with SigV4 + CRIS ids."""

    def __init__(self, http_client: httpx.AsyncClient, signer: _Signer) -> None:
        self._http = http_client
        self._signer = signer

    def _url(self, endpoint: str, wire: str) -> str:
        try:
            suffix = _WIRE_PATHS[wire]
        except KeyError:
            raise ValueError(
                f"Unknown OpenAI wire {wire!r}; expected one of {sorted(_WIRE_PATHS)}"
            ) from None
        return f"{endpoint.rstrip('/')}{suffix}"

    async def _prepare(
        self, request_body: bytes, model_id: str, endpoint: str, profile, wire: str
    ) -> tuple[str, dict]:
        url = self._url(endpoint, wire)
        if not model_id.startswith(_CRIS_PREFIXES):
            # Not fatal here: the service answers with a precise ValidationException that
            # is more useful to an operator than a guess from us, and a future model may
            # be ON_DEMAND. Logged so a mis-registered alias is diagnosable from the logs
            # instead of only from a client's 400.
            logger.warning("bedrock_openai_model_id_not_cris", model_id=model_id)
        headers = await self._signer.sign(
            method="POST",
            url=url,
            body=request_body,
            region=_region_for(endpoint, profile),
            role_arn=getattr(profile, "account_role_arn", None),
            external_id=getattr(profile, "external_id", None),
        )
        return url, headers

    async def invoke(
        self,
        request_body: bytes,
        model_id: str,
        *,
        profile=None,
        endpoint: str = "",
        wire: str = "responses",
        **kwargs,
    ) -> tuple[int, bytes, dict, TokenUsage]:
        try:
            url, headers = await self._prepare(request_body, model_id, endpoint, profile, wire)
        except Exception:
            logger.exception("bedrock_openai_sign_failed", model_id=model_id, wire=wire)
            return (
                502,
                b'{"error":{"type":"provider_error","message":"Bedrock (OpenAI) signing failed"}}',
                {},
                TokenUsage(),
            )
        try:
            resp = await self._http.post(url, content=request_body, headers=headers)
        except Exception:
            logger.exception("bedrock_openai_invoke_failed", model_id=model_id, wire=wire)
            return (
                502,
                b'{"error":{"type":"provider_error","message":"Bedrock (OpenAI) call failed"}}',
                {},
                TokenUsage(),
            )

        body = resp.content
        # x-amzn-requestid is the join key to the Bedrock model-invocation log record for
        # this call, so it is logged on BOTH the success and the error path — an audit that
        # can only correlate successful requests is not an audit.
        #
        # It is ALSO returned in the headers dict so the router can persist it as
        # usage_logs.bedrock_request_id. The headers dict (not a 5th tuple element) because
        # ProviderAdapter.invoke is a 4-tuple across every adapter and the streaming
        # variant already carries the id in its 4th slot; widening the non-streaming
        # contract would touch adapters that have no request id to give.
        aws_request_id = resp.headers.get("x-amzn-requestid")
        out_headers = {"x-amzn-requestid": aws_request_id} if aws_request_id else {}
        if resp.status_code != 200:
            logger.warning(
                "bedrock_openai_http_error",
                status=resp.status_code,
                model_id=model_id,
                wire=wire,
                aws_request_id=aws_request_id,
                error_body=body[:500].decode("utf-8", "replace"),
            )
            return resp.status_code, body, out_headers, TokenUsage()

        logger.info(
            "bedrock_openai_invoked",
            model_id=model_id,
            wire=wire,
            aws_request_id=aws_request_id,
            streaming=False,
        )
        try:
            parsed = json.loads(body)
            usage = (
                extract_responses_usage(parsed)
                if wire == "responses"
                else extract_chat_usage(parsed.get("usage") or {})
            )
        except Exception:
            usage = TokenUsage()
        return 200, body, out_headers, usage

    async def invoke_stream(
        self,
        request_body: bytes,
        model_id: str,
        *,
        profile=None,
        endpoint: str = "",
        wire: str = "responses",
        **kwargs,
    ) -> tuple[int, AsyncIterator[bytes], dict, str | None]:
        try:
            url, headers = await self._prepare(request_body, model_id, endpoint, profile, wire)
        except Exception:
            logger.exception("bedrock_openai_stream_sign_failed", model_id=model_id, wire=wire)

            async def _auth_err() -> AsyncIterator[bytes]:
                yield _error_chunk(wire, "Bedrock (OpenAI) signing failed")

            return 502, _auth_err(), {}, None

        # Open the stream and read the status BEFORE returning, so a non-200 surfaces as
        # the real HTTP status instead of a 200 with an error buried in the body
        # (same contract as MantleOpenAIAdapter.invoke_stream).
        cm = self._http.stream("POST", url, content=request_body, headers=headers)
        try:
            resp = await cm.__aenter__()
        except Exception:
            logger.exception("bedrock_openai_stream_connect_failed", model_id=model_id, wire=wire)

            async def _conn_err() -> AsyncIterator[bytes]:
                yield _error_chunk(wire, "Bedrock (OpenAI) stream failed")

            return 502, _conn_err(), {}, None

        aws_request_id = resp.headers.get("x-amzn-requestid")
        if resp.status_code != 200:
            status = resp.status_code
            err_body = b""
            try:
                err_body = await resp.aread()
            except Exception:
                pass
            logger.warning(
                "bedrock_openai_stream_http_error",
                status=status,
                model_id=model_id,
                wire=wire,
                aws_request_id=aws_request_id,
                error_body=err_body[:500].decode("utf-8", "replace"),
            )
            await cm.__aexit__(None, None, None)

            async def _http_err() -> AsyncIterator[bytes]:
                yield _error_chunk(wire, f"Bedrock (OpenAI) stream HTTP {status}")

            # The id IS returned on this path, matching `invoke`: AWS answered, so a log
            # record may exist for the rejected call and an audit that can only correlate
            # successful requests is not an audit. The sign/connect failures above return
            # None instead because there was no response to take an id from — that is
            # "no record to join to", not a dropped id.
            return status, _http_err(), {}, aws_request_id

        logger.info(
            "bedrock_openai_invoked",
            model_id=model_id,
            wire=wire,
            aws_request_id=aws_request_id,
            streaming=True,
        )

        # The two wires have DIFFERENT downstream contracts and must not share a generator:
        #
        #   responses → services/streaming.responses_sse_stream RE-FRAMES, so it wants the
        #               bare JSON of each `data:` line (same contract as MantleOpenAIAdapter).
        #   chat      → services/streaming.openai_sse_stream PASSES THROUGH, so it wants the
        #               upstream bytes untouched; its usage scanner looks for `data: ` lines.
        #
        # Verified live 2026-09-03 that bedrock-runtime frames chat chunks as `data: {json}`
        # (with the space openai_sse_stream requires) and terminates with `data: [DONE]`,
        # so raw passthrough is byte-compatible with the vLLM path already in production.
        async def _gen_responses() -> AsyncIterator[bytes]:
            # ⚠️ 종료 프레임을 이미 넘겼는지 기억한다 — 근거는 openai_usage 의 같은 섹션
            #    주석(완료된 응답 뒤에 오류 프레임을 붙이면 Codex 가 재시도한다).
            terminal_seen = False
            try:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        if is_terminal_responses_payload(payload):
                            terminal_seen = True
                        if payload and payload != "[DONE]":
                            yield payload.encode()
            except Exception:
                if terminal_seen:
                    # 응답은 완결됐고 예외는 연결 정리 단계에서 났다. 오류 프레임을 붙이면
                    # 이미 받은 성공을 실패로 덮는다.
                    logger.warning(
                        "bedrock_openai_stream_teardown_after_terminal",
                        model_id=model_id, wire=wire, exc_info=True,
                    )
                else:
                    logger.exception(
                        "bedrock_openai_stream_failed", model_id=model_id, wire=wire
                    )
                    yield _error_chunk(wire, "Bedrock (OpenAI) stream failed")
            finally:
                await cm.__aexit__(None, None, None)

        async def _gen_chat() -> AsyncIterator[bytes]:
            terminal_seen = False
            try:
                async for chunk in resp.aiter_bytes():
                    if chat_chunk_is_terminal(chunk):
                        terminal_seen = True
                    yield chunk
            except Exception:
                if terminal_seen:
                    logger.warning(
                        "bedrock_openai_stream_teardown_after_terminal",
                        model_id=model_id, wire=wire, exc_info=True,
                    )
                else:
                    logger.exception(
                        "bedrock_openai_stream_failed", model_id=model_id, wire=wire
                    )
                    yield _error_chunk(wire, "Bedrock (OpenAI) stream failed")
            finally:
                await cm.__aexit__(None, None, None)

        gen = _gen_responses() if wire == "responses" else _gen_chat()
        return 200, gen, {"Content-Type": "text/event-stream"}, aws_request_id

    async def count_tokens(self, request_body: bytes, model_id: str, **kwargs) -> tuple[int, int]:
        # The OpenAI wires expose no token-count endpoint, and Bedrock's CountTokens
        # operation does not accept these models (it takes an invokeModel/converse body).
        # 0 keeps the /count_tokens router's contract (status, input_tokens) without
        # inventing a number.
        return 200, 0
