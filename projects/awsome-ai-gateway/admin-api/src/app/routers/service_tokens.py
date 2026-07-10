# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.core.exceptions import ForbiddenError, ValidationError
from app.schemas.service_tokens import (
    ServiceTokenCreateRequest,
    ServiceTokenCreateResponse,
    ServiceTokenListResponse,
)

router = APIRouter(prefix="/admin/service-tokens", tags=["Service Token Management"])


@router.post("", response_model=ServiceTokenCreateResponse)
async def issue_service_token(
    request: Request,
    body: ServiceTokenCreateRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """발급 — 사람 admin(JWT)만. 원문은 응답에서 1회만 반환."""
    if admin.is_service_token:
        raise ForbiddenError("Service tokens cannot issue new service tokens")
    svc = request.app.state.service_token_service
    return await svc.issue(
        session, name=body.name, created_by=admin.user_id, expiry_days=body.expiry_days
    )


@router.get("", response_model=ServiceTokenListResponse)
async def list_service_tokens(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """목록 — prefix만, 원문 없음."""
    svc = request.app.state.service_token_service
    return await svc.list_tokens(session)


@router.post("/rotate", response_model=ServiceTokenCreateResponse)
async def rotate_service_token(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """self-rotate — service token 호출자만. 자기 자신을 교체, 구 토큰 24h grace."""
    if not admin.is_service_token or admin.service_token_id is None:
        raise ValidationError("Rotate is only callable by a service token authenticating itself")
    svc = request.app.state.service_token_service
    return await svc.rotate(
        session, token_id=admin.service_token_id, created_by=admin.user_id
    )


@router.delete("/{token_id}", status_code=204)
async def revoke_service_token(
    request: Request,
    token_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """폐기 — 사람 admin(JWT)만. 즉시 무효화."""
    if admin.is_service_token:
        raise ForbiddenError("Service tokens cannot revoke service tokens")
    svc = request.app.state.service_token_service
    await svc.revoke(session, token_id=uuid.UUID(token_id))
