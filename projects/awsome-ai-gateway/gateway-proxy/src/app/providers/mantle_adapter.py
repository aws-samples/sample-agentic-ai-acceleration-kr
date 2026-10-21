# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Protocol

import httpx
import structlog

from app.providers.base import CountTokensResult, ProviderAdapter
from app.providers.bedrock_adapter import _extract_bedrock_usage  # reuse usage parsing
from app.schemas.domain import TokenUsage
from app.services.account_credentials import (
    ACCOUNT_UNAVAILABLE_BODY,
    ACCOUNT_UNAVAILABLE_CODE,
    ACCOUNT_UNAVAILABLE_MESSAGE,
    ACCOUNT_UNAVAILABLE_STATUS,
    AccountCredentialsUnavailable,
    redact_account_identifiers,
    redact_account_identifiers_bytes,
)

logger = structlog.get_logger(__name__)

_ANTHROPIC_VERSION = "2023-06-01"
# connect stays under the cut-stream estimate's 2.5 s budget (services/tokenizer.py),
# so an unreachable endpoint is reported — and remembered — rather than cancelled.
_COUNT_TIMEOUT = httpx.Timeout(connect=1.5, read=10.0, write=10.0, pool=5.0)


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
            resp = await self._http.post(url, content=request_body, headers=headers)
        except AccountCredentialsUnavailable:
            # alias 계정을 쓸 수 없다 — 502(폴백·서킷 대상)가 아니라 고정 500. 이 타입은 alias
            # 계정 mint 에서만 나온다(프로필·파드 경로의 실패는 아래 기존 502 그대로).
            return ACCOUNT_UNAVAILABLE_STATUS, ACCOUNT_UNAVAILABLE_BODY, {}, TokenUsage()
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

    async def count_tokens(
        self,
        request_body: bytes,
        model_id: str,
        *,
        profile=None,
        endpoint: str | None = None,
        account=None,
        **kwargs,
    ) -> CountTokensResult:
        """Mantle ``/v1/messages/count_tokens`` — the count for the model the body names. No cost.

        Used only as the fallback for models that Bedrock CountTokens refuses
        (services/token_count_fallback.py), which reads the result as: 200 counted ·
        404 not served in this region · 403 not entitled to this model · 400/422 this
        model refused this body · 401 or ``error_code`` ``MantleAuthFailed`` /
        ``AccountCredentialsUnavailable`` the credentials · ``MantleUnreachable`` could
        not connect · ``MantleCallFailed`` or anything else this request only.
        """
        if profile is None or not endpoint:
            # This used to return (200, 0): any caller would have recorded a 0-token count.
            logger.error("mantle_count_tokens_misconfigured", model_id=model_id)
            return CountTokensResult(
                500, 0, "MantleCountMisconfigured",
                "Mantle count_tokens needs a routing profile and an endpoint",
            )
        url = f"{endpoint.rstrip('/')}/v1/messages/count_tokens"
        try:
            headers = await self._headers(profile, account)
        except AccountCredentialsUnavailable:
            return CountTokensResult(
                ACCOUNT_UNAVAILABLE_STATUS, 0, ACCOUNT_UNAVAILABLE_CODE,
                ACCOUNT_UNAVAILABLE_MESSAGE,
            )
        except Exception:
            logger.exception("mantle_count_tokens_auth_failed", model_id=model_id)
            return CountTokensResult(401, 0, "MantleAuthFailed", "Mantle auth failed")
        try:
            # Its own short timeout: the shared client's read timeout is sized for
            # generation, and a count that hangs would hold the caller for minutes.
            resp = await self._http.post(
                url, content=request_body, headers=headers, timeout=_COUNT_TIMEOUT
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.warning(
                "mantle_count_tokens_unreachable", model_id=model_id, error=type(exc).__name__
            )
            return CountTokensResult(502, 0, "MantleUnreachable", "Mantle endpoint unreachable")
        except Exception as exc:
            logger.warning(
                "mantle_count_tokens_failed", model_id=model_id, error=type(exc).__name__
            )
            return CountTokensResult(502, 0, "MantleCallFailed", "Mantle count_tokens call failed")
        if resp.status_code != 200:
            message = redact_account_identifiers(resp.text[:500])
            logger.warning(
                "mantle_count_tokens_http_error",
                status=resp.status_code, model_id=model_id, error_body=message,
            )
            return CountTokensResult(resp.status_code, 0, f"HTTP{resp.status_code}", message)
        try:
            input_tokens = int(resp.json()["input_tokens"])
        except Exception:
            logger.warning("mantle_count_tokens_bad_response", model_id=model_id)
            return CountTokensResult(
                502, 0, "MantleBadResponse", "unexpected Mantle count_tokens response"
            )
        return CountTokensResult(200, input_tokens)
