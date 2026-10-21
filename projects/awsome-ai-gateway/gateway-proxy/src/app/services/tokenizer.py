# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from botocore.exceptions import ClientError

from app.schemas.domain import ModelConfigSchema, ProviderType, TokenUsage
from app.services.token_count_fallback import (
    BEDROCK_UNSUPPORTED,
    count_base_model_id,
    is_count_unsupported,
)

logger = structlog.get_logger(__name__)


#: 이 길이까지는 루프에서 바로 센다(~1ms). 그보다 긴 텍스트는 스레드로(estimate_output_tokens).
_INLINE_TOKENIZE_MAX_CHARS = 32_768
#: Mantle 카운트에 줄 시간. 넘기면 tiktoken 근사로 간다(_mantle_count_bounded).
_MANTLE_COUNT_BUDGET_SECONDS = 2.5

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

    ⚠️ Bedrock CountTokens 는 Opus 4.7+ 와 5 계열을 세지 못한다(모델 기능, 권한 무관 —
       services/token_count_fallback.py). 그 모델들의 잘린 스트림은 tiktoken 으로 떨어지는데,
       cl100k 는 새 토크나이저보다 영어를 ~45% 적게 센다(같은 본문 547 vs ~300) — 과소청구다.
       ``mantle_count`` 가 주어지면(COUNT_TOKENS_MANTLE_FALLBACK) 그 앞에서 Mantle 로 센다.
    """

    def __init__(
        self,
        bedrock_client: Any | None = None,
        mantle_count: Callable[[str, str], Awaitable[int | None]] | None = None,
    ) -> None:
        self._bedrock_client = bedrock_client
        self._mantle_count = mantle_count
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
            if self._mantle_count is not None and (
                count_base_model_id(_strip_region_prefix(model_id)) in BEDROCK_UNSUPPORTED
            ):
                n = await self._mantle_count_bounded(text, model_id)
                if n is not None:
                    return n
            logger.info("bedrock_count_tokens_fallback_to_tiktoken", model_id=model_id)

        if len(text) <= _INLINE_TOKENIZE_MAX_CHARS:
            return self._tiktoken_count(text)
        # ⚠️ 큰 텍스트(잘린 스트림의 프롬프트 추정)는 루프 밖에서 센다. tiktoken 은 GIL 을 놓고
        #    돌아서 스레드로 옮기면 루프가 멈추지 않는다 — 실측: 1M 토큰 인라인 252ms 정지,
        #    to_thread 5.6ms. SIGTERM 은 모든 스트림을 한꺼번에 자르므로 인라인이면 이 정지가
        #    drain 수만큼 겹친다.
        return await asyncio.to_thread(self._tiktoken_count, text)

    async def _mantle_count_bounded(self, text: str, model_id: str) -> int | None:
        """Mantle count, bounded. The cut-stream finalize writes the usage row only after
        this estimate, and a shutdown drains for ~12 s — a slow Mantle must cost an
        approximation, never the row."""
        try:
            return await asyncio.wait_for(
                self._mantle_count(text, model_id), timeout=_MANTLE_COUNT_BUDGET_SECONDS
            )
        except Exception:
            logger.warning("mantle_count_tokens_estimate_failed", model_id=model_id)
            return None

    async def _bedrock_count_tokens(self, text: str, model_id: str) -> int | None:
        """Bedrock CountTokens API 호출. 실패 시 `None`."""
        if self._bedrock_client is None:
            return None

        base_model = count_base_model_id(_strip_region_prefix(model_id))
        if base_model in BEDROCK_UNSUPPORTED:
            return None
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
        except ClientError as e:
            err = e.response.get("Error", {})
            if is_count_unsupported(err.get("Code"), err.get("Message")):
                BEDROCK_UNSUPPORTED.add(base_model)
            logger.warning(
                "bedrock_count_tokens_failed", model_id=base_model, error_code=err.get("Code")
            )
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


# ── 잘린 스트림의 프롬프트 추정 (KI-08 의 입력 쪽) ───────────────────────────────

#: 값이 프롬프트 텍스트가 아닌 필드.
#:   · base64 페이로드 — Chat input_audio.data, Anthropic source.data, Converse bytes,
#:     Responses/Chat file_data(data URL 은 접두사로 따로 거른다)
#:   · 불투명 재전송 값 — Responses reasoning.encrypted_content(무상태 클라이언트가 매 턴 다시
#:     보낸다: 8,000 자가 5,749 토큰으로 세어졌다, 리뷰 프로브로 실측), thinking 의 signature,
#:     항목 id
_NON_PROMPT_KEYS = frozenset(
    {"data", "bytes", "file_data", "encrypted_content", "signature", "id"}
)
#: 프롬프트를 이루는 최상위 필드(Responses / Chat / Anthropic). model·max_tokens 등은 아니다.
_PROMPT_KEYS = ("instructions", "system", "messages", "input", "tools")


def prompt_text(body: dict) -> str:
    """프롬프트 토큰 역산용 텍스트 — 대화·지시·도구 정의의 문자열 값만 모은다.

    ⚠️ base64 미디어와 불투명 재전송 값(encrypted_content·signature·id)은 뺀다(_NON_PROMPT_KEYS).
       이미지는 해상도 기반 수백~수천 토큰인데 base64 를 토크나이저에 넣으면 수십만 토큰으로
       부풀어 과청구가 된다. 대가로 이미지 토큰과 JSON 구조 토큰은 0 으로 센다 — 추정은
       아래쪽으로 기운다(estimate_prompt_usage 의 하한 원칙과 같은 방향).
    """
    parts: list[str] = []

    def walk(node: Any, key: str | None) -> None:
        if isinstance(node, str):
            if key not in _NON_PROMPT_KEYS and not node.startswith("data:"):
                parts.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)

    for key in _PROMPT_KEYS:
        if key in body:
            walk(body[key], key)
    return "\n".join(parts)


async def estimate_prompt_usage(
    tokenizer: TokenizerService | None, body: Any, model_config: ModelConfigSchema
) -> TokenUsage | None:
    """usage 이벤트 없이 끊긴 스트림의 프롬프트 usage 추정 — **하한**으로 청구한다.

    Responses / chat 방언은 usage 를 종결 이벤트에만 싣는다. 그 전에 끊기면(ESC, SIGTERM
    셧다운, 상류 절단) 이미 처리돼 AWS 가 청구한 프롬프트가 0 으로 남는다.

    ⚠️ 어느 버킷으로 청구하나: 캐시 히트가 얼마였는지는 종결 이벤트에만 있다. 전부 비캐시 입력으로
       잡으면 캐시가 대부분인 프롬프트(Codex 같은 에이전트 루프)가 최대 ~10배 과청구된다. 그래서
       모델이 캐시 읽기 단가를 매기면 그 버킷으로 잡는다 — 어떤 프롬프트 토큰도 캐시 읽기보다
       싸게 청구되지 않으므로 실제 청구의 **하한**이다. 캐시 단가가 없는 모델은 캐시될 수 없으니
       입력으로 잡는다.
    """
    if tokenizer is None or not isinstance(body, dict):
        return None
    text = prompt_text(body)
    if not text:
        return None
    # estimate_output_tokens 는 "텍스트 → 토큰 수" 일 뿐이다(이름은 KI-08 의 출력 쪽 용도에서 왔다).
    n = await tokenizer.estimate_output_tokens(
        text, provider=model_config.provider, model_id=model_config.provider_model_id
    )
    if not n or n <= 0:
        return None
    if model_config.pricing.cache_read_per_1k > 0:
        return TokenUsage(cache_read_input_tokens=n)
    return TokenUsage(input_tokens=n)
