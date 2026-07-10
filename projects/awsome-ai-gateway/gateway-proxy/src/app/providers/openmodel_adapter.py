# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import structlog

from app.providers.base import ProviderAdapter
from app.schemas.domain import TokenUsage

logger = structlog.get_logger(__name__)


def _extract_usage_from_chunk(chunk_data: dict) -> TokenUsage | None:
    """OpenAI SSE 마지막 chunk에서 usage 추출."""
    usage = chunk_data.get("usage")
    if usage:
        return TokenUsage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
    return None


class OpenModelAdapter(ProviderAdapter):
    """사내 오픈모델 (httpx, OpenAI-compatible) adapter."""

    def __init__(self, httpx_client: httpx.AsyncClient, base_url: str) -> None:
        self._client = httpx_client
        self._base_url = base_url.rstrip("/")

    def _build_request_body(self, raw_body: bytes, model_id: str, stream: bool) -> dict:
        """요청 body 파싱 + stream_options 강제 삽입."""
        body = json.loads(raw_body)
        body["model"] = model_id  # alias → provider_model_id
        if stream:
            body["stream"] = True
            # stream_options.include_usage=true 강제 삽입
            if "stream_options" not in body:
                body["stream_options"] = {"include_usage": True}
            elif not body["stream_options"].get("include_usage"):
                body["stream_options"]["include_usage"] = True
        return body

    async def invoke(
        self, request_body: bytes, model_id: str, path: str = "/v1/chat/completions", **kwargs
    ) -> tuple[int, bytes, dict, TokenUsage]:
        try:
            body = self._build_request_body(request_body, model_id, stream=False)
            response = await self._client.post(
                f"{self._base_url}{path}",
                json=body,
            )
            response_body = response.content
            usage = TokenUsage()
            if response.status_code == 200:
                try:
                    parsed = json.loads(response_body)
                    raw_usage = parsed.get("usage", {})
                    usage = TokenUsage(
                        input_tokens=raw_usage.get("prompt_tokens", 0),
                        output_tokens=raw_usage.get("completion_tokens", 0),
                        total_tokens=raw_usage.get("total_tokens", 0),
                    )
                except Exception:
                    pass
            return response.status_code, response_body, dict(response.headers), usage

        except httpx.TimeoutException:
            logger.warning("openmodel_timeout", model_id=model_id)
            return (
                504,
                b'{"error":{"type":"provider_error","message":"Provider timeout"}}',
                {},
                TokenUsage(),
            )
        except Exception:
            logger.exception("openmodel_invoke_failed", model_id=model_id)
            return (
                502,
                b'{"error":{"type":"provider_error","message":"OpenModel call failed"}}',
                {},
                TokenUsage(),
            )

    async def invoke_stream(
        self, request_body: bytes, model_id: str, path: str = "/v1/chat/completions", **kwargs
    ) -> tuple[int, AsyncIterator[bytes], dict]:
        try:
            body = self._build_request_body(request_body, model_id, stream=True)
            request = self._client.build_request(
                "POST",
                f"{self._base_url}{path}",
                json=body,
            )
            response = await self._client.send(request, stream=True)

            return (
                response.status_code,
                self._sse_stream_gen(response),
                {
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                },
            )
        except Exception:
            logger.exception("openmodel_stream_failed", model_id=model_id)

            async def error_gen():
                yield b'data: {"error":{"type":"provider_error","message":"Stream failed"}}\n\n'

            return 502, error_gen(), {}

    async def _sse_stream_gen(self, response: httpx.Response) -> AsyncIterator[bytes]:
        """SSE 청크를 클라이언트에 그대로 yield."""
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        except Exception:
            logger.exception("sse_stream_error")
        finally:
            await response.aclose()

    @staticmethod
    def estimate_input_tokens(messages: list[dict]) -> int:
        """입력 토큰 추정 (사전 검증용): 문자수 / 4."""
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        return max(1, total_chars // 4)
