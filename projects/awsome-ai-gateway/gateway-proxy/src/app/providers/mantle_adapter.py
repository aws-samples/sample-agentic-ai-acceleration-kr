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
from app.services.account_credentials import (
    ACCOUNT_UNAVAILABLE_BODY,
    ACCOUNT_UNAVAILABLE_STATUS,
    CREDENTIAL_FAILURE_MARKER,
    AccountCredentialsUnavailable,
    redact_account_identifiers_bytes,
)

logger = structlog.get_logger(__name__)

_ANTHROPIC_VERSION = "2023-06-01"


class _BearerProvider(Protocol):
    async def bearer_token(self, profile, account=...) -> str: ...


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

    async def _headers(self, profile, account=None) -> dict:
        # alias 계정(0043)이 있을 때만 넘긴다 — 없으면 오늘 호출 그대로(브로커가 프로필 계정,
        # 그것도 없으면 파드 자격증명으로 mint).
        if account is not None:
            token = await self._broker.bearer_token(profile, account)
        else:
            token = await self._broker.bearer_token(profile)
        return {
            "Authorization": f"Bearer {token}",
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def invoke(
        self,
        request_body: bytes,
        model_id: str,
        *,
        profile,
        endpoint: str,
        account=None,
        **kwargs,
    ) -> tuple[int, bytes, dict, TokenUsage]:
        url = f"{endpoint.rstrip('/')}/v1/messages"
        try:
            headers = await self._headers(profile, account)
        except AccountCredentialsUnavailable:
            # alias 계정을 쓸 수 없다 — 502(폴백·서킷 대상)가 아니라 고정 500. 이 타입은 alias
            # 계정 mint 에서만 나온다(프로필·파드 경로의 실패는 아래 기존 502 그대로).
            return ACCOUNT_UNAVAILABLE_STATUS, ACCOUNT_UNAVAILABLE_BODY, {}, TokenUsage()
        except Exception:
            # bearer 를 만들지 못했다(프로필 역할의 AssumeRole 실패·ProfileAssumeBackoff 등) —
            # 상류를 부르지 않았다. 오늘과 같은 502 지만 서킷 브레이커에는 세지 않는다
            # (CREDENTIAL_FAILURE_MARKER).
            logger.exception("mantle_invoke_failed", model_id=model_id, stage="auth")
            return (
                502,
                b'{"error":{"type":"provider_error","message":"Mantle call failed"}}',
                {CREDENTIAL_FAILURE_MARKER: "1"},
                TokenUsage(),
            )
        try:
            resp = await self._http.post(url, content=request_body, headers=headers)
        except Exception:
            logger.exception("mantle_invoke_failed", model_id=model_id, stage="call")
            return (
                502,
                b'{"error":{"type":"provider_error","message":"Mantle call failed"}}',
                {},
                TokenUsage(),
            )

        body = resp.content
        if resp.status_code != 200:
            logger.warning(
                "mantle_http_error",
                status=resp.status_code,
                model_id=model_id,
                error_body=body[:500].decode("utf-8", "replace"),
            )
            # 원문은 위 로그에만 — 클라이언트로 가는 본문에서는 서명 계정의 ARN·번호를 지운다.
            return resp.status_code, redact_account_identifiers_bytes(body), {}, TokenUsage()

        try:
            usage = _extract_bedrock_usage(json.loads(body))
        except Exception:
            usage = TokenUsage()
        return 200, body, {}, usage

    async def invoke_stream(
        self,
        request_body: bytes,
        model_id: str,
        *,
        profile,
        endpoint: str,
        account=None,
        **kwargs,
    ) -> tuple[int, AsyncIterator[bytes], dict, str | None]:
        url = f"{endpoint.rstrip('/')}/v1/messages"
        try:
            headers = await self._headers(profile, account)
        except AccountCredentialsUnavailable:

            async def _account_err() -> AsyncIterator[bytes]:
                yield ACCOUNT_UNAVAILABLE_BODY

            return ACCOUNT_UNAVAILABLE_STATUS, _account_err(), {}, None
        except Exception:
            logger.exception("mantle_stream_auth_failed", model_id=model_id)

            async def _auth_err() -> AsyncIterator[bytes]:
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": "Mantle auth failed"}}
                ).encode()

            # 상류를 부르지 않았다 — 서킷 브레이커에 세지 않는다(invoke 의 같은 주석).
            return 502, _auth_err(), {CREDENTIAL_FAILURE_MARKER: "1"}, None

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
            err_body = b""
            try:
                err_body = await resp.aread()
            except Exception:
                pass
            logger.warning(
                "mantle_stream_http_error",
                status=status,
                model_id=model_id,
                error_body=err_body[:500].decode("utf-8", "replace"),
            )
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
