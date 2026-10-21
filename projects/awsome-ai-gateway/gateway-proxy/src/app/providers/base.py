# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from app.schemas.domain import TokenUsage


class CountTokensResult(tuple):
    """``(status, input_tokens)`` — a plain 2-tuple to every existing caller.

    The upstream error code and message ride along as attributes so the
    count_tokens router can tell "this model cannot be counted" apart from an
    auth or throttling failure, without changing the tuple every adapter and
    test unpacks.
    """

    error_code: str | None
    error_message: str | None

    def __new__(
        cls,
        status: int,
        input_tokens: int,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> CountTokensResult:
        self = super().__new__(cls, (status, input_tokens))
        self.error_code = error_code
        self.error_message = error_message
        return self

    def __getnewargs__(self):
        # copy / deepcopy / pickle rebuild through __new__, which needs every field.
        return (self[0], self[1], self.error_code, self.error_message)


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
