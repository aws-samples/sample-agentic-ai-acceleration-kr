# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from typing import TYPE_CHECKING

from app.schemas.domain import ProviderType

if TYPE_CHECKING:
    from app.providers.base import ProviderAdapter


class ProviderRegistry:
    """Provider 유형별 Adapter 레지스트리."""

    def __init__(self) -> None:
        self._adapters: dict[ProviderType, ProviderAdapter] = {}

    def register(self, provider_type: ProviderType, adapter: ProviderAdapter) -> None:
        self._adapters[provider_type] = adapter

    def get(self, provider_type: ProviderType) -> ProviderAdapter:
        adapter = self._adapters.get(provider_type)
        if adapter is None:
            raise ValueError(f"No adapter registered for provider: {provider_type}")
        return adapter
