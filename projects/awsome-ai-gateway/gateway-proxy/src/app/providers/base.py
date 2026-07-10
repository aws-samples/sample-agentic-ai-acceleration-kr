# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from app.schemas.domain import TokenUsage


class ProviderAdapter(ABC):
    """Provider Adapter 기본 클래스."""

    @abstractmethod
    async def invoke(
        self, request_body: bytes, model_id: str, **kwargs
    ) -> tuple[int, bytes, dict, TokenUsage]:
        """비스트리밍 호출.
        Returns: (status_code, body, headers, token_usage)
        """
        ...

    @abstractmethod
    async def invoke_stream(
        self, request_body: bytes, model_id: str, **kwargs
    ) -> tuple[int, AsyncIterator[bytes], dict, str | None]:
        """스트리밍 호출.
        Returns: (status_code, chunk_iterator, headers, request_id_or_none)
        TokenUsage는 스트림 종료 시 chunk_iterator에서 추출.
        """
        ...
