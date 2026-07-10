# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Schemas for OIDC auth endpoints (POST /v1/auth/exchange)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OIDCExchangeRequest(BaseModel):
    """JWT → VK 교환 요청 본문 (Authorization 헤더에 Bearer JWT)."""

    device_name: str = Field(default="cli", max_length=255)
    sso_session_expires_at: datetime | None = None
