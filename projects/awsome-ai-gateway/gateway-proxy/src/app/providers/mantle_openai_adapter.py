# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Protocol

import httpx
import structlog

from app.providers.base import ProviderAdapter
from app.schemas.domain import TokenUsage

logger = structlog.get_logger(__name__)


class _BearerProvider(Protocol):
    async def bearer_token(self, profile) -> str: ...


def _extract_responses_usage(response_body: dict) -> TokenUsage:
    """Parse a Bedrock-Mantle OpenAI **Responses API** usage object into TokenUsage.

    Responses usage shape (verified live against GPT-5.5):
        usage.input_tokens
        usage.output_tokens                         # ALREADY includes reasoning tokens
        usage.total_tokens
        usage.input_tokens_details.cached_tokens    # prompt-cache hits
        usage.output_tokens_details.reasoning_tokens

    reasoning_tokens is recorded as a VISIBILITY SUBMETRIC only — it is NOT added to
    output/total/cost again (it is already inside output_tokens per OpenAI accounting).
    cached_tokens maps to cache_read_input_tokens (read-side cache, like Anthropic).
    """
    usage = response_body.get("usage") or {}
    in_details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=int(in_details.get("cached_tokens", 0) or 0),
        reasoning_tokens=int(out_details.get("reasoning_tokens", 0) or 0),
    )


class MantleOpenAIAdapter(ProviderAdapter):
    """Bedrock **Mantle** adapter for the OpenAI **Responses API** + bearer token.

    Targets POST {endpoint}/v1/responses on bedrock-mantle.{region}.api.aws/openai
    using a short-lived bearer minted by MantleCredentialBroker. Unlike MantleAdapter
    (Anthropic Messages), this speaks the OpenAI Responses wire: no anthropic-version
    header, Responses-shaped usage, and Responses SSE events (response.output_text.delta
    for text, response.completed carrying final usage). Used for Codex -> Ohio GPT-5.5.

    Codex's account == the gateway IRSA account (859), so the broker takes the
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
            logger.warning("mantle_openai_http_error", status=resp.status_code, model_id=model_id)
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
            try:
                await resp.aread()
            except Exception:
                pass
            await cm.__aexit__(None, None, None)

            async def _http_err() -> AsyncIterator[bytes]:
                yield json.dumps(
                    {"error": {"type": "provider_error",
                               "message": f"Mantle (OpenAI) stream HTTP {status}"}}
                ).encode()

            return status, _http_err(), {}, None

        async def _gen() -> AsyncIterator[bytes]:
            try:
                async for line in resp.aiter_lines():
                    # Responses SSE: "data: {json}" lines carry typed events
                    # (response.output_text.delta, response.completed, ...). Emit the
                    # JSON payload so the downstream responses SSE stream re-formats it.
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        if payload and payload != "[DONE]":
                            yield payload.encode()
            except Exception:
                logger.exception("mantle_openai_stream_failed", model_id=model_id)
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": "Mantle (OpenAI) stream failed"}}
                ).encode()
            finally:
                await cm.__aexit__(None, None, None)

        return 200, _gen(), {"Content-Type": "text/event-stream"}, None

    async def count_tokens(self, request_body: bytes, model_id: str, **kwargs) -> tuple[int, int]:
        # Responses API has no separate count endpoint used here; not required for Codex MVP.
        return 200, 0
