# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.schemas.models import (
    AwsPricePreviewResponse,
    AwsPriceSyncRequest,
    AwsPriceSyncResponse,
    ModelCreateRequest,
    ModelListResponse,
    ModelResponse,
    ModelUpdateRequest,
    PricingRequest,
    StatusPatchRequest,
)

router = APIRouter(prefix="/admin/models", tags=["Model Management"])


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


@router.get("/pricing/aws-preview", response_model=AwsPricePreviewResponse)
async def aws_price_preview(
    request: Request,
    region_code: str = "us-east-1",
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """AWS Price List standard 단가 vs DB 현재가 필드별 drift 미리보기(읽기 전용, 쓰기 없음).

    운영자가 이 diff 를 확인한 뒤 aws-sync 로 명시 적용. 자동 적용 없음. region_code 는 대조할
    단가 리전(Price List API 엔드포인트 us-east-1 과는 별개) — alias 의 실제 서빙 리전을 넘긴다.
    """
    from app.services.model_service import ModelService

    svc: ModelService = request.app.state.model_service
    return await svc.preview_aws_pricing(session, region_code=region_code)


@router.post("/pricing/aws-sync", response_model=AwsPriceSyncResponse)
async def aws_price_sync(
    request: Request,
    body: AwsPriceSyncRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """승인된 alias 목록만 AWS 단가로 반영(기존 set_pricing 재사용 — 시계열·감사·캐시·long 승계)."""
    from app.services.model_service import ModelService

    svc: ModelService = request.app.state.model_service
    return await svc.sync_aws_pricing(
        session,
        aliases=body.aliases,
        region_code=body.region_code,
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
