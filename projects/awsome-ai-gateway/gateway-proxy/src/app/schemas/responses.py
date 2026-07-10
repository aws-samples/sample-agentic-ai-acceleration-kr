# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    type: str
    message: str
    code: str
    retry_after: Optional[int] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthComponent(BaseModel):
    status: str  # "up" | "down"


class HealthResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "unhealthy"
    components: dict[str, HealthComponent]
    degradation_level: str


class UsageBudgetInfo(BaseModel):
    max_usd: Decimal
    used_usd: Decimal
    remaining_usd: Decimal
    pct: float
    policy: str


class UsageByModel(BaseModel):
    model_id: str
    total_tokens: int
    total_cost_usd: Decimal
    request_count: int


class DailyBreakdown(BaseModel):
    date: str
    total_tokens: int
    total_cost_usd: Decimal
    by_model: list[UsageByModel] = []


class UsageMeResponse(BaseModel):
    user_id: str
    period: str
    usage: dict[str, Any]
    budget: UsageBudgetInfo
    daily_breakdown: list[DailyBreakdown] = []


class ModelPricingObject(BaseModel):
    input_per_1k_usd: Decimal
    output_per_1k_usd: Decimal
    currency: str = "USD"


class ModelObject(BaseModel):
    # Anthropic 공식 `/v1/models` 포맷 (Claude Code 가 이걸 기대):
    #   { "type": "model", "id": ..., "display_name": ..., "created_at": ISO8601 }
    # OpenAI 호환 필드 (`object`, `created`, `owned_by`) 도 유지해서 기존 OpenAI SDK 클라이언트도 동작.
    type: str = "model"
    id: str
    display_name: Optional[str] = None
    created_at: Optional[str] = None  # ISO8601
    object: str = "model"
    created: int
    owned_by: str = "gateway"
    provider: str
    api_format: str
    provider_model_id: str
    description: Optional[str] = None
    pricing: Optional[ModelPricingObject] = None


class ModelsListResponse(BaseModel):
    # Anthropic 포맷 필드 (has_more, first_id, last_id) + OpenAI 호환 (object=list)
    data: list[ModelObject]
    has_more: bool = False
    first_id: Optional[str] = None
    last_id: Optional[str] = None
    object: str = "list"
