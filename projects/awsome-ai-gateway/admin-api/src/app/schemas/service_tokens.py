# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ServiceTokenCreateRequest(BaseModel):
    name: str
    expiry_days: int | None = Field(None, description="Override default expiry (days)")


class ServiceTokenCreateResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    token: str = Field(description="Plaintext token — returned only on issue/rotate")


class ServiceTokenItem(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class ServiceTokenListResponse(BaseModel):
    items: list[ServiceTokenItem]
