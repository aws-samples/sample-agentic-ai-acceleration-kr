# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from botocore.exceptions import ClientError

from app.providers.base import ProviderAdapter
from app.schemas.domain import TokenUsage

logger = structlog.get_logger(__name__)

# boto3 예외 → HTTP 상태 코드 매핑
_BOTO_ERROR_MAP: dict[str, int] = {
    "ValidationException": 400,
    "AccessDeniedException": 403,
    "ResourceNotFoundException": 404,
    "ThrottlingException": 429,
    "ModelTimeoutException": 504,
    "ServiceException": 502,
    "InternalServerException": 502,
}


def _extract_bedrock_usage(response_body: dict) -> TokenUsage:
    """Bedrock invoke/converse 응답에서 TokenUsage 추출 (cache 토큰 포함)."""
    usage = response_body.get("usage", {})
    input_tokens = usage.get("input_tokens", usage.get("inputTokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("outputTokens", 0))
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


class BedrockAdapter(ProviderAdapter):
    """AWS Bedrock Runtime adapter (boto3 기반).

    두 모드:
    - in-account(기본): bedrock_client 고정. resolver/fallback None → 기존과 동일 동작.
    - cross-account(claude-code→374): client_resolver = async () -> boto3 client
      (STS assume 후 대상 계정 bedrock-runtime). assume/build 실패 시 fallback_client
      (in-account 859)로 **투명 폴백** → claude-code 절대 안 죽음(사용자 결정).
    """

    def __init__(self, bedrock_client, client_resolver=None, fallback_client=None) -> None:
        self._client = bedrock_client
        self._client_resolver = client_resolver
        self._fallback_client = fallback_client

    async def _get_client(self):
        """resolver 있으면 cross-account 클라이언트, 실패 시 in-account fallback."""
        if self._client_resolver is None:
            return self._client
        try:
            return await self._client_resolver()
        except Exception:
            logger.warning("bedrock_xacct_resolve_failed_fallback_inaccount", exc_info=True)
            if self._fallback_client is not None:
                return self._fallback_client
            raise

    async def invoke(
        self, request_body: bytes, model_id: str, path_suffix: str = "invoke", **kwargs
    ) -> tuple[int, bytes, dict, TokenUsage]:
        import asyncio

        loop = asyncio.get_event_loop()
        try:
            client = await self._get_client()
            if path_suffix in ("invoke", ""):
                response = await loop.run_in_executor(
                    None,
                    lambda: client.invoke_model(
                        modelId=model_id,
                        body=request_body,
                        contentType="application/json",
                        accept="application/json",
                    ),
                )
                body = response["body"].read()
                try:
                    parsed = json.loads(body)
                    usage = _extract_bedrock_usage(parsed)
                except Exception:
                    usage = TokenUsage()
                return 200, body, {}, usage

            elif path_suffix == "converse":
                parsed_req = json.loads(request_body)
                response = await loop.run_in_executor(
                    None,
                    lambda: client.converse(
                        modelId=model_id,
                        **{k: v for k, v in parsed_req.items() if k != "modelId"},
                    ),
                )
                usage = TokenUsage(
                    input_tokens=response.get("usage", {}).get("inputTokens", 0),
                    output_tokens=response.get("usage", {}).get("outputTokens", 0),
                )
                usage.total_tokens = usage.input_tokens + usage.output_tokens
                body = json.dumps(response).encode()
                return 200, body, {}, usage

        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = _BOTO_ERROR_MAP.get(code, 502)
            logger.warning("bedrock_client_error", error_code=code, model_id=model_id)
            error_body = json.dumps(
                {"error": {"type": "provider_error", "message": str(e)}}
            ).encode()
            return status, error_body, {}, TokenUsage()
        except Exception:
            logger.exception("bedrock_invoke_failed", model_id=model_id)
            return (
                502,
                b'{"error":{"type":"provider_error","message":"Bedrock call failed"}}',
                {},
                TokenUsage(),
            )

    async def count_tokens(self, request_body: bytes, model_id: str) -> tuple[int, int]:
        """Bedrock CountTokens API — returns (status, input_tokens). No cost, no inference."""
        import asyncio

        loop = asyncio.get_event_loop()
        try:
            client = await self._get_client()
            response = await loop.run_in_executor(
                None,
                lambda: client.count_tokens(
                    modelId=model_id,
                    input={"invokeModel": {"body": request_body}},
                ),
            )
            return 200, int(response.get("inputTokens", 0))
        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = _BOTO_ERROR_MAP.get(code, 502)
            logger.warning("bedrock_count_tokens_error", error_code=code, model_id=model_id)
            return status, 0
        except Exception:
            logger.exception("bedrock_count_tokens_failed", model_id=model_id)
            return 502, 0

    async def invoke_stream(
        self,
        request_body: bytes,
        model_id: str,
        path_suffix: str = "invoke-with-response-stream",
        **kwargs,
    ) -> tuple[int, AsyncIterator[bytes], dict, str | None]:
        import asyncio

        loop = asyncio.get_event_loop()
        try:
            client = await self._get_client()
            if path_suffix == "invoke-with-response-stream":
                response = await loop.run_in_executor(
                    None,
                    lambda: client.invoke_model_with_response_stream(
                        modelId=model_id,
                        body=request_body,
                        contentType="application/json",
                        accept="application/json",
                    ),
                )
                aws_request_id: str | None = response.get("ResponseMetadata", {}).get("RequestId")
                stream = response.get("body")
                return (
                    200,
                    self._bedrock_stream_gen(stream),
                    {"Content-Type": "application/vnd.amazon.eventstream"},
                    aws_request_id,
                )

            elif path_suffix == "converse-stream":
                parsed_req = json.loads(request_body)
                response = await loop.run_in_executor(
                    None,
                    lambda: client.converse_stream(
                        modelId=model_id,
                        **{k: v for k, v in parsed_req.items() if k != "modelId"},
                    ),
                )
                aws_request_id = response.get("ResponseMetadata", {}).get("RequestId")
                stream = response.get("stream")
                return (
                    200,
                    self._converse_stream_gen(stream),
                    {"Content-Type": "application/vnd.amazon.eventstream"},
                    aws_request_id,
                )

        except ClientError as e:
            code = e.response["Error"]["Code"]
            status = _BOTO_ERROR_MAP.get(code, 502)
            error_msg = str(e)
            logger.warning(
                "bedrock_stream_client_error", error_code=code, model_id=model_id, error=error_msg
            )

            async def error_gen():
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": error_msg}}
                ).encode()

            return status, error_gen(), {}, None
        except Exception as exc:
            error_msg = str(exc)
            logger.exception("bedrock_stream_failed", model_id=model_id)

            async def error_gen():
                yield json.dumps(
                    {"error": {"type": "provider_error", "message": error_msg}}
                ).encode()

            return 502, error_gen(), {}, None

    async def _bedrock_stream_gen(self, stream) -> AsyncIterator[bytes]:
        """Adapt botocore's blocking EventStream iterator to an async generator.

        Each `next()` call can block waiting for the next event to arrive over
        the wire, so we execute it in the default thread pool via
        `run_in_executor` to keep the event loop responsive.

        Errors propagate to the caller so the streaming helper can surface
        them as SSE `event: error` to the client rather than silently cutting
        the connection.
        """
        import asyncio

        loop = asyncio.get_event_loop()
        it = iter(stream)
        sentinel = object()

        def _next():
            try:
                return next(it)
            except StopIteration:
                return sentinel

        while True:
            event = await loop.run_in_executor(None, _next)
            if event is sentinel:
                return
            chunk = event.get("chunk", {})
            if "bytes" in chunk:
                yield chunk["bytes"]

    async def _converse_stream_gen(self, stream) -> AsyncIterator[bytes]:
        import asyncio

        loop = asyncio.get_event_loop()
        it = iter(stream)
        sentinel = object()

        def _next():
            try:
                return next(it)
            except StopIteration:
                return sentinel

        while True:
            event = await loop.run_in_executor(None, _next)
            if event is sentinel:
                return
            yield json.dumps(event).encode()
