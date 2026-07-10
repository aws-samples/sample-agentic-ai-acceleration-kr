# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Protocol

import httpx
import structlog

from app.providers.base import ProviderAdapter
from app.providers.bedrock_adapter import _extract_bedrock_usage  # reuse usage parsing
from app.schemas.domain import TokenUsage

logger = structlog.get_logger(__name__)

_ANTHROPIC_VERSION = "2023-06-01"


class _BearerProvider(Protocol):
    async def bearer_token(self, profile) -> str: ...


class MantleAdapter(ProviderAdapter):
    """Bedrock **Mantle** adapter — Anthropic Messages over HTTPS + bearer token.

    NOT boto3 invoke_model. Targets POST {endpoint}/v1/messages on
    bedrock-mantle.{region}.api.aws/anthropic using a short-lived bearer minted
    by MantleCredentialBroker (which assumes the cross-account 905 role).
    Used for Cowork -> Tokyo Opus 4.8.
    """

    def __init__(self, http_client: httpx.AsyncClient, broker: _BearerProvider) -> None:
        self._http = http_client
        self._broker = broker

    async def _headers(self, profile) -> dict:
        token = await self._broker.bearer_token(profile)
        return {
            "Authorization": f"Bearer {token}",
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def invoke(
        self, request_body: bytes, model_id: str, *, profile, endpoint: str, **kwargs
    ) -> tuple[int, bytes, dict, TokenUsage]:
        url = f"{endpoint.rstrip('/')}/v1/messages"
        try:
            headers = await self._headers(profile)
            resp = await self._http.post(url, content=request_body, headers=headers)
        except Exception:
            logger.exception("mantle_invoke_failed", model_id=model_id)
            return (
                502,
                b'{"error":{"type":"provider_error","message":"Mantle call failed"}}',
                {},
                TokenUsage(),
            )

        body = resp.content
        if resp.status_code != 200:
            logger.warning("mantle_http_error", status=resp.status_code, model_id=model_id)
            return resp.status_code, body, {}, TokenUsage()

        try:
            usage = _extract_bedrock_usage(json.loads(body))
        except Exception:
            usage = TokenUsage()
        return 200, body, {}, usage

    async def invoke_stream(
        self, request_body: bytes, model_id: str, *, profile, endpoint: str, **kwargs
    ) -> tuple[int, AsyncIterator[bytes], dict, str | None]:
        url = f"{endpoint.rstrip('/')}/v1/messages"
        try:
            headers = await self._headers(profile)
        except Exception:
            logger.exception("mantle_stream_auth_failed", model_id=model_id)

            async def _auth_err() -> AsyncIterator[bytes]:
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": "Mantle auth failed"}}
                ).encode()

            return 502, _auth_err(), {}, None

        # Open the stream and read the status BEFORE returning, so a non-200 Mantle
        # response surfaces as the real HTTP status (not a 200 with a buried error).
        cm = self._http.stream("POST", url, content=request_body, headers=headers)
        try:
            resp = await cm.__aenter__()
        except Exception:
            logger.exception("mantle_stream_connect_failed", model_id=model_id)

            async def _conn_err() -> AsyncIterator[bytes]:
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": "Mantle stream failed"}}
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
                               "message": f"Mantle stream HTTP {status}"}}
                ).encode()

            return status, _http_err(), {}, None

        async def _gen() -> AsyncIterator[bytes]:
            try:
                async for line in resp.aiter_lines():
                    # Anthropic SSE: "data: {json}" lines carry events; emit the
                    # JSON payload so bedrock_anthropic_sse_stream re-formats it.
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        if payload and payload != "[DONE]":
                            yield payload.encode()
            except Exception:
                logger.exception("mantle_stream_failed", model_id=model_id)
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": "Mantle stream failed"}}
                ).encode()
            finally:
                await cm.__aexit__(None, None, None)

        return 200, _gen(), {"Content-Type": "text/event-stream"}, None

    async def count_tokens(self, request_body: bytes, model_id: str, **kwargs) -> tuple[int, int]:
        # Mantle exposes /v1/messages/count_tokens; not required for Cowork MVP.
        # The router only calls count_tokens on the Bedrock path for Phase 2.
        return 200, 0
