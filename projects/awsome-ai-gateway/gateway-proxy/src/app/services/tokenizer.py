# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog

from app.schemas.domain import ProviderType

logger = structlog.get_logger(__name__)


# Cross-region inference profile 접두사 제거용 (예: `us.anthropic.claude-...` → `anthropic.claude-...`).
# Bedrock CountTokens API는 profile ID를 받지 않고 base model ID만 허용.
_REGION_PREFIX_RE = re.compile(r"^(us|eu|apac|global|us-gov)\.")


def _strip_region_prefix(model_id: str) -> str:
    return _REGION_PREFIX_RE.sub("", model_id)


class TokenizerService:
    """스트리밍 disconnect 시 partial output 토큰 역산 (KI-08).

    - Bedrock (Anthropic Claude): `bedrock-runtime count_tokens` API 호출
      (서버사이드, 정확). cross-region profile → base ID strip 처리.
    - OpenAI-compatible (vLLM 등): `tiktoken` `cl100k_base` 인코딩 기반 근사
      (모델 무관 ±10% 수준).

    모든 경로가 실패하면 ``None`` 반환 → cost_recorder는 0 토큰으로 기록.
    """

    def __init__(self, bedrock_client: Any | None = None) -> None:
        self._bedrock_client = bedrock_client
        self._tiktoken_encoding = None

    def _get_tiktoken(self):
        if self._tiktoken_encoding is None:
            try:
                import tiktoken

                self._tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
            except Exception:
                logger.warning("tiktoken_load_failed")
                self._tiktoken_encoding = False  # sentinel — don't retry
        return self._tiktoken_encoding or None

    async def estimate_output_tokens(
        self,
        text: str,
        *,
        provider: ProviderType,
        model_id: str,
    ) -> int | None:
        """누적된 output 텍스트 → 토큰 수 역산.

        Bedrock Anthropic은 CountTokens API, 그 외는 tiktoken.
        `text`가 비어있으면 0 반환.
        실패 시 `None` — 호출자는 0 토큰 + `estimated_usage=True`로 처리.
        """
        if not text:
            return 0

        if provider == ProviderType.BEDROCK:
            n = await self._bedrock_count_tokens(text, model_id)
            if n is not None:
                return n
            logger.info("bedrock_count_tokens_fallback_to_tiktoken", model_id=model_id)

        return self._tiktoken_count(text)

    async def _bedrock_count_tokens(self, text: str, model_id: str) -> int | None:
        """Bedrock CountTokens API 호출. 실패 시 `None`."""
        if self._bedrock_client is None:
            return None

        base_model = _strip_region_prefix(model_id)
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1,  # CountTokens는 생성 안 하나 스키마상 필수값.
            "messages": [{"role": "user", "content": text}],
        }

        def _call() -> dict:
            return self._bedrock_client.count_tokens(
                modelId=base_model,
                input={"invokeModel": {"body": json.dumps(body).encode("utf-8")}},
            )

        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, _call)
            count = resp.get("inputTokens") or resp.get("input", {}).get("inputTokens")
            if isinstance(count, int) and count >= 0:
                return count
            logger.warning("bedrock_count_tokens_unexpected_shape", keys=list(resp.keys()))
            return None
        except Exception:
            logger.warning("bedrock_count_tokens_failed", model_id=base_model)
            return None

    def _tiktoken_count(self, text: str) -> int | None:
        """tiktoken `cl100k_base` 근사. Claude/Llama/Mistral 모두 ±10% 수준."""
        enc = self._get_tiktoken()
        if enc is None:
            return None
        try:
            return len(enc.encode(text))
        except Exception:
            logger.warning("tiktoken_encode_failed")
            return None
