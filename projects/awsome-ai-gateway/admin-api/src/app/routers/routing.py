# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Routing profiles admin router — per-client web search toggle.

GET  /admin/routing-profiles                → list all clients + web_search_enabled
PUT  /admin/routing-profiles/{client}/web-search  body {enabled: bool}

Mirrors the users.py allowed-clients pattern (require_admin, per-request service,
cache invalidation, session.commit). The web_search_enabled column already exists
(migration 0021) — no migration needed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.services.routing_profile_service import RoutingProfileService

router = APIRouter(prefix="/admin/routing-profiles", tags=["Routing Profiles"])


class WebSearchToggleBody(BaseModel):
    enabled: bool


class RoutingProfileItem(BaseModel):
    client: str
    web_search_enabled: bool
    backend: str
    enabled: bool


class RoutingProfileListResponse(BaseModel):
    items: list[RoutingProfileItem]


@router.get("", response_model=RoutingProfileListResponse)
async def list_routing_profiles(
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = RoutingProfileService(session)
    items = await svc.list_profiles()
    return RoutingProfileListResponse(items=[RoutingProfileItem(**i) for i in items])


@router.put("/{client}/web-search")
async def set_web_search(
    request: Request,
    client: str,
    body: WebSearchToggleBody,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc = RoutingProfileService(session, cache_mgr=request.app.state.cache_mgr)
    try:
        result = await svc.set_web_search(client, body.enabled, admin.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await session.commit()
    return result
