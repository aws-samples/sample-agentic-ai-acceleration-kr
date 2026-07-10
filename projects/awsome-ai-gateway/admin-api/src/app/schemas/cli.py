# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


# ── Requests ──


class STSRequestPayload(BaseModel):
    url: str
    headers: dict[str, str]


class VirtualKeyIssueRequest(BaseModel):
    sts_request: STSRequestPayload
    device_name: str = Field(max_length=255)
    sso_session_expires_at: datetime | None = None


class SetupRequest(BaseModel):
    device_name: str = Field(max_length=255)
    os: str = Field(description="darwin | linux | windows")
    arch: str = Field(description="amd64 | arm64")
    detected_tools: list[str] = []
    components: list[str] = Field(default=["all"])


# ── Responses ──


class VirtualKeyIssueResponse(BaseModel):
    virtual_key: str
    expires_at: datetime
    gateway_endpoint: str
    otel_endpoint: str
    user_id: str
    team_id: str | None = None
    max_budget_usd: Decimal | None = None
    used_usd: Decimal | None = None
    tpm_limit: int | None = None
    rpm_limit: int | None = None


class ToolConfig(BaseModel):
    type: str
    auth: str | None = None
    use_api_key_helper: bool | None = None


class SetupResponse(BaseModel):
    bedrock_endpoint: str
    openai_endpoint: str
    otel_endpoint: str
    tool_configs: dict[str, ToolConfig] = {}
