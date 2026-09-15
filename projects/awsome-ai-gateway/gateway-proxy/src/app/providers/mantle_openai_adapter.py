# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Protocol

import httpx
import structlog

from app.providers.base import ProviderAdapter
from app.providers.openai_usage import extract_responses_usage, is_terminal_responses_payload
from app.schemas.domain import TokenUsage

logger = structlog.get_logger(__name__)


class _BearerProvider(Protocol):
    async def bearer_token(self, profile) -> str: ...


# The Responses usage parser now lives in providers/openai_usage.py — one implementation
# shared by this adapter, the bedrock-runtime adapter and both streaming finalizers, so a
# streamed request and a non-streamed request of the same prompt can never bill
# differently. Re-exported under the original private name because it is the documented
# reference point for the cache-billing regression suite
# (tests/regression/test_high_cached_token_double_billing.py) and db/versions/0025.
_extract_responses_usage = extract_responses_usage


class MantleOpenAIAdapter(ProviderAdapter):
    """Bedrock **Mantle** adapter for the OpenAI **Responses API** + bearer token.

    Targets POST {endpoint}/v1/responses on bedrock-mantle.{region}.api.aws/openai
    using a short-lived bearer minted by MantleCredentialBroker. Unlike MantleAdapter
    (Anthropic Messages), this speaks the OpenAI Responses wire: no anthropic-version
    header, Responses-shaped usage, and Responses SSE events (response.output_text.delta
    for text, response.completed carrying final usage). Used for Codex -> Ohio GPT-5.5.

    Codex's account == the gateway IRSA account (123), so the broker takes the
    in-account credential path (routing_profiles.account_role_arn IS NULL).
    """

    def __init__(self, http_client: httpx.AsyncClient, broker: _BearerProvider) -> None:
        self._http = http_client
        self._broker = broker

    async def _headers(self, profile) -> dict:
        token = await self._broker.bearer_token(profile)
        return {
            "Authorization": f"Bearer {token}",
            "content-type": "application/json",
        }

    async def invoke(
        self, request_body: bytes, model_id: str, *, profile, endpoint: str, **kwargs
    ) -> tuple[int, bytes, dict, TokenUsage]:
        url = f"{endpoint.rstrip('/')}/v1/responses"
        try:
            headers = await self._headers(profile)
            resp = await self._http.post(url, content=request_body, headers=headers)
        except Exception:
            logger.exception("mantle_openai_invoke_failed", model_id=model_id)
            return (
                502,
                b'{"error":{"type":"provider_error","message":"Mantle (OpenAI) call failed"}}',
                {},
                TokenUsage(),
            )

        body = resp.content
        if resp.status_code != 200:
            logger.warning(
                "mantle_openai_http_error",
                status=resp.status_code,
                model_id=model_id,
                error_body=body[:500].decode("utf-8", "replace"),
            )
            return resp.status_code, body, {}, TokenUsage()

        try:
            usage = _extract_responses_usage(json.loads(body))
        except Exception:
            usage = TokenUsage()
        return 200, body, {}, usage

    async def invoke_stream(
        self, request_body: bytes, model_id: str, *, profile, endpoint: str, **kwargs
    ) -> tuple[int, AsyncIterator[bytes], dict, str | None]:
        url = f"{endpoint.rstrip('/')}/v1/responses"
        try:
            headers = await self._headers(profile)
        except Exception:
            logger.exception("mantle_openai_stream_auth_failed", model_id=model_id)

            async def _auth_err() -> AsyncIterator[bytes]:
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": "Mantle (OpenAI) auth failed"}}
                ).encode()

            return 502, _auth_err(), {}, None

        # Open the stream and read status BEFORE returning, so a non-200 surfaces as
        # the real HTTP status (not a 200 with a buried error).
        cm = self._http.stream("POST", url, content=request_body, headers=headers)
        try:
            resp = await cm.__aenter__()
        except Exception:
            logger.exception("mantle_openai_stream_connect_failed", model_id=model_id)

            async def _conn_err() -> AsyncIterator[bytes]:
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": "Mantle (OpenAI) stream failed"}}
                ).encode()

            return 502, _conn_err(), {}, None

        if resp.status_code != 200:
            status = resp.status_code
            err_body = b""
            try:
                err_body = await resp.aread()
            except Exception:
                pass
            logger.warning(
                "mantle_openai_stream_http_error",
                status=status,
                model_id=model_id,
                error_body=err_body[:500].decode("utf-8", "replace"),
            )
            await cm.__aexit__(None, None, None)

            async def _http_err() -> AsyncIterator[bytes]:
                yield json.dumps(
                    {"error": {"type": "provider_error",
                               "message": f"Mantle (OpenAI) stream HTTP {status}"}}
                ).encode()

            return status, _http_err(), {}, None

        async def _gen() -> AsyncIterator[bytes]:
            # ⚠️ 종료 프레임 이후의 예외에는 오류 프레임을 붙이지 않는다 — 근거는
            #    openai_usage 의 같은 섹션 주석. runtime 어댑터와 **같은** 판정을 쓴다.
            terminal_seen = False
            try:
                async for line in resp.aiter_lines():
                    # Responses SSE: "data: {json}" lines carry typed events
                    # (response.output_text.delta, response.completed, ...). Emit the
                    # JSON payload so the downstream responses SSE stream re-formats it.
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        if is_terminal_responses_payload(payload):
                            terminal_seen = True
                        if payload and payload != "[DONE]":
                            yield payload.encode()
            except Exception:
                if terminal_seen:
                    logger.warning(
                        "mantle_openai_stream_teardown_after_terminal",
                        model_id=model_id, exc_info=True,
                    )
                else:
                    logger.exception("mantle_openai_stream_failed", model_id=model_id)
                    yield json.dumps(
                        {"error": {"type": "provider_error",
                                   "message": "Mantle (OpenAI) stream failed"}}
                    ).encode()
            finally:
                await cm.__aexit__(None, None, None)

        return 200, _gen(), {"Content-Type": "text/event-stream"}, None

    async def count_tokens(self, request_body: bytes, model_id: str, **kwargs) -> tuple[int, int]:
        # Responses API has no separate count endpoint used here; not required for Codex MVP.
        return 200, 0
