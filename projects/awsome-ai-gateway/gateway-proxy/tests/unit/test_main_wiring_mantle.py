# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

from app.providers.mantle_adapter import MantleAdapter
from app.providers.registry import ProviderRegistry
from app.schemas.domain import ProviderType


def test_registry_can_hold_mantle_adapter():
    reg = ProviderRegistry()
    fake = MantleAdapter(http_client=object(), broker=object())
    reg.register(ProviderType.BEDROCK_MANTLE, fake)
    assert reg.get(ProviderType.BEDROCK_MANTLE) is fake
