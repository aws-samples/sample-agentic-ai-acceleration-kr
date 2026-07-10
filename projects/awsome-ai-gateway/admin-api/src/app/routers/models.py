# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.config import get_settings
from app.core.db import get_db_session
from app.schemas.models import (
    ModelCreateRequest,
    ModelListResponse,
    ModelResponse,
    ModelUpdateRequest,
    PriceSyncApplyRequest,
    PriceSyncApplyResponse,
    PriceSyncPreviewResponse,
    PricingRequest,
    StatusPatchRequest,
)

router = APIRouter(prefix="/admin/models", tags=["Model Management"])


def _build_pricing_sync_service():
    """AWS Price List API(boto3 pricing client, us-east-1) 기반 동기화 서비스 생성.

    가격 동기화 소스는 AWS Price List API 이며 AgentCore Gateway/Inference Targets 아님
    (IT 는 단가를 노출하지 않음). region 은 Price List 전용 엔드포인트(us-east-1 등).
    """
    import boto3

    from app.services.pricing_sync_service import PricingSyncService

    settings = get_settings()
    region = settings.PRICING_API_REGION
    client = boto3.client("pricing", region_name=region)
    svc = PricingSyncService(client)
    svc.region = region  # preview 응답에 표시
    return svc


@router.get("", response_model=ModelListResponse)
async def list_models(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.model_service import ModelService

    svc: ModelService = request.app.state.model_service
    items = await svc.list_models(session)
    return ModelListResponse(items=items)


@router.post("", response_model=ModelResponse, status_code=201)
async def create_model(
    request: Request,
    body: ModelCreateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: ModelService = request.app.state.model_service
    return await svc.create_model(
        session,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.put("/{alias}", response_model=ModelResponse)
async def update_model(
    request: Request,
    alias: str,
    body: ModelUpdateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: ModelService = request.app.state.model_service
    return await svc.update_model(
        session,
        alias=alias,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.put("/{alias}/pricing", response_model=ModelResponse)
async def set_pricing(
    request: Request,
    alias: str,
    body: PricingRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: ModelService = request.app.state.model_service
    return await svc.set_pricing(
        session,
        alias=alias,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.get("/pricing/sync-preview", response_model=PriceSyncPreviewResponse)
async def price_sync_preview(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """AWS Price List 단가 vs DB 현재가 diff 미리보기(읽기 전용, 쓰기 없음).

    운영자가 이 diff 를 확인한 뒤 sync-apply 로 명시 적용. 자동 적용 없음.
    """
    from app.services.model_service import ModelService

    svc: ModelService = request.app.state.model_service
    pricing_sync = _build_pricing_sync_service()
    return await svc.preview_price_sync(session, pricing_sync_service=pricing_sync)


@router.post("/pricing/sync-apply", response_model=PriceSyncApplyResponse)
async def price_sync_apply(
    request: Request,
    body: PriceSyncApplyRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """승인된 alias 목록만 AWS 단가로 적용(기존 set_pricing 재사용 — 시계열·감사·캐시)."""
    from app.services.model_service import ModelService

    svc: ModelService = request.app.state.model_service
    pricing_sync = _build_pricing_sync_service()
    return await svc.apply_price_sync(
        session,
        pricing_sync_service=pricing_sync,
        aliases=body.aliases,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.patch("/{alias}/status", response_model=ModelResponse)
async def patch_status(
    request: Request,
    alias: str,
    body: StatusPatchRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: ModelService = request.app.state.model_service
    return await svc.patch_status(
        session,
        alias=alias,
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
