# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.common import ApiFormatEnum, ProviderEnum


# ── Requests ──


class ModelCreateRequest(BaseModel):
    alias: str = Field(max_length=128)
    provider: ProviderEnum
    provider_model_id: str = Field(max_length=512)
    endpoint_url: str | None = None
    api_format: ApiFormatEnum
    description: str | None = None
    display_name: str | None = Field(default=None, max_length=128)
    input_price_per_1k_tokens: Decimal = Field(ge=0, decimal_places=6)
    output_price_per_1k_tokens: Decimal = Field(ge=0, decimal_places=6)
    cache_creation_5m_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, decimal_places=6
    )
    cache_creation_1h_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, decimal_places=6
    )
    cache_read_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, decimal_places=6
    )


class ModelUpdateRequest(BaseModel):
    provider_model_id: str | None = None
    endpoint_url: str | None = None
    description: str | None = None
    # max_length matches VARCHAR(128); without it an overlong update would 500 at the DB
    # instead of a clean 422 (mirrors ModelCreateRequest.display_name).
    # NOTE: update uses an is-not-None filter, so display_name can be SET/changed but not
    # cleared back to NULL via the API (repo-wide behavior for all nullable update fields).
    display_name: str | None = Field(default=None, max_length=128)


class PricingRequest(BaseModel):
    input_price_per_1k_tokens: Decimal = Field(ge=0, decimal_places=6)
    output_price_per_1k_tokens: Decimal = Field(ge=0, decimal_places=6)
    cache_creation_5m_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, decimal_places=6
    )
    cache_creation_1h_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, decimal_places=6
    )
    cache_read_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, decimal_places=6
    )
    effective_from: datetime


class StatusPatchRequest(BaseModel):
    active: bool


# ── Responses ──


class ModelPricingResponse(BaseModel):
    input_price_per_1k_tokens: Decimal
    output_price_per_1k_tokens: Decimal
    cache_creation_5m_price_per_1k_tokens: Decimal = Decimal("0")
    cache_creation_1h_price_per_1k_tokens: Decimal = Decimal("0")
    cache_read_price_per_1k_tokens: Decimal = Decimal("0")
    effective_from: datetime
    effective_until: datetime | None = None


class ModelResponse(BaseModel):
    alias: str
    provider: ProviderEnum
    provider_model_id: str
    endpoint_url: str | None = None
    api_format: ApiFormatEnum
    status: str
    description: str | None = None
    display_name: str | None = None
    current_pricing: ModelPricingResponse | None = None
    created_at: datetime
    updated_at: datetime


class ModelListResponse(BaseModel):
    items: list[ModelResponse]


# ── Price sync (AWS Price List API 동기화) ──


class PriceSyncDiff(BaseModel):
    """모델 1개의 현재 단가 vs AWS 공식 단가 diff(미리보기 전용, 쓰기 없음)."""

    alias: str
    provider_model_id: str
    matched: bool  # AWS Price List 에서 단가를 찾았나
    note: str | None = None  # 미매칭/주의 사유
    current: ModelPricingResponse | None = None  # DB 현재가(없을 수 있음)
    # AWS 에서 가져와 per-1k 정규화한 제안 단가(매칭 시)
    proposed_input_per_1k: Decimal | None = None
    proposed_output_per_1k: Decimal | None = None
    proposed_cache_5m_per_1k: Decimal | None = None
    proposed_cache_1h_per_1k: Decimal | None = None
    proposed_cache_read_per_1k: Decimal | None = None
    changed: bool = False  # 현재가와 제안가가 다른가


class PriceSyncPreviewResponse(BaseModel):
    source: str = "aws_price_list_api"  # 출처 명시(IT 아님)
    region: str
    diffs: list[PriceSyncDiff]
    matched_count: int
    changed_count: int


class PriceSyncApplyRequest(BaseModel):
    """승인 후 적용할 alias 목록(명시 선택 — 자동 전체적용 금지)."""

    aliases: list[str] = Field(min_length=1)


class PriceSyncApplyResponse(BaseModel):
    applied: list[str]
    skipped: list[str]
    errors: list[str] = Field(default_factory=list)


# ── Team Allowed Models ──


class AllowedModelsSetRequest(BaseModel):
    """Replace-all semantics: provided list becomes the new full whitelist.

    빈 리스트 = 전체 허용 (엔트리 전부 삭제).
    """

    model_aliases: list[str] = Field(default_factory=list)


class AllowedModelsResponse(BaseModel):
    team_id: str
    model_aliases: list[str]
