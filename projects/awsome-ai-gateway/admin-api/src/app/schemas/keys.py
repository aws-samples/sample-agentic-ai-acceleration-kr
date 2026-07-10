# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import KeyStatus, PaginatedResponse


# ── Responses ──


class KeyResponse(BaseModel):
    key_id: str
    key_prefix: str
    user_id: str
    user_email: str | None = None
    status: KeyStatus
    issued_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    created_at: datetime


class KeyCreateResponse(BaseModel):
    key_id: str
    key_prefix: str
    user_id: str
    status: KeyStatus
    created_at: datetime
    expires_at: datetime
    virtual_key: str | None = Field(None, description="Plaintext VK returned only on creation")


class KeyListResponse(PaginatedResponse[KeyResponse]):
    pass


class KeyCountResponse(BaseModel):
    count: int
