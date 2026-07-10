# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ── Enums ──


class UserRole(str, enum.Enum):
    ADMIN = "ADMIN"
    TEAM_LEADER = "TEAM_LEADER"
    DEVELOPER = "DEVELOPER"


class KeyStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class BudgetPolicy(str, enum.Enum):
    HARD_BLOCK = "HARD_BLOCK"
    SOFT_WARNING = "SOFT_WARNING"
    THROTTLE = "THROTTLE"


class ProviderEnum(str, enum.Enum):
    BEDROCK = "BEDROCK"
    OPENMODEL = "OPENMODEL"
    BEDROCK_MANTLE = "BEDROCK_MANTLE"  # Cowork → 905 Bedrock Mantle (Tokyo Opus 4.8)
    BEDROCK_MANTLE_OPENAI = "BEDROCK_MANTLE_OPENAI"  # Codex → 859 Bedrock Mantle GPT-5.5 (Ohio)


class ApiFormatEnum(str, enum.Enum):
    BEDROCK_NATIVE = "BEDROCK_NATIVE"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"
    ANTHROPIC_MESSAGES = "ANTHROPIC_MESSAGES"  # Mantle /anthropic/v1/messages
    OPENAI_RESPONSES = "OPENAI_RESPONSES"  # Mantle /openai/v1/responses (GPT-5.x)


class ScopeEnum(str, enum.Enum):
    USER = "USER"
    TEAM = "TEAM"
    GLOBAL = "GLOBAL"


# ── Pagination ──


class PaginationParams(BaseModel):
    cursor: str | None = Field(None, description="Last item ID for cursor-based pagination")
    limit: int = Field(50, ge=1, le=200, description="Number of items per page")


class PaginationMeta(BaseModel):
    cursor: str | None = None
    limit: int
    has_more: bool


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    pagination: PaginationMeta


# ── Error ──


class ErrorDetail(BaseModel):
    type: str
    message: str
    code: str
    retry_after: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
