# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


# ── Query Params ──


class AnalyticsQueryParams(BaseModel):
    period: str = Field(description="YYYY-MM format")
    group_by: str = Field("model", description="model | team | department | user")
    scope: str = Field("all", description="all | team:{id}")


class ExportParams(BaseModel):
    format: str = Field("csv", description="csv | json")
    period: str = Field(description="YYYY-MM format")
    group_by: str = Field("model")


# ── Responses ──


class CostSummary(BaseModel):
    total_requests: int = 0
    total_tokens: int = 0
    total_cost_usd: Decimal = Decimal("0")
    active_users: int = 0
    avg_cost_per_user_usd: Decimal = Decimal("0")


class ModelBreakdown(BaseModel):
    model: str
    requests: int = 0
    cost_usd: Decimal = Decimal("0")


class TeamBreakdown(BaseModel):
    team: str
    team_id: str
    cost_usd: Decimal = Decimal("0")
    active_users: int = 0


class UserBreakdown(BaseModel):
    user: str  # display_name (PII=sso_subject 미노출)
    email: str
    cost_usd: Decimal = Decimal("0")
    requests: int = 0


class TrendItem(BaseModel):
    date: str
    cost_usd: Decimal = Decimal("0")
    requests: int = 0


class AnalyticsResponse(BaseModel):
    period: str
    currency: str = "USD"
    cost_summary: CostSummary
    by_model: list[ModelBreakdown] = []
    by_team: list[TeamBreakdown] = []
    by_user: list[UserBreakdown] = []
    trends: list[TrendItem] = []


class UsageByUserModelItem(BaseModel):
    date: str
    user_id: str
    user_name: str | None = None
    model_alias: str
    cost_usd: Decimal
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    department_id: str | None = None
    department_name: str | None = None
    team_id: str | None = None
    team_name: str | None = None
    avg_latency_ms: int


class UsageByUserModelResponse(BaseModel):
    period: str
    date: str
    items: list[UsageByUserModelItem]


class UsageByUserItem(BaseModel):
    date: str
    user_id: str
    user_name: str | None = None
    cost_usd: Decimal
    calls: int
    department_id: str | None = None
    department_name: str | None = None
    team_id: str | None = None
    team_name: str | None = None


class UsageByUserResponse(BaseModel):
    period: str
    date: str
    items: list[UsageByUserItem]
