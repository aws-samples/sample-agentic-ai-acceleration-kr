# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin, require_admin_or_team_leader
from app.core.db import get_db_session
from app.models.budget import BudgetScope
from app.schemas.budgets import (
    AllocateBudgetRequest,
    AutoDowngradeConfigRequest,
    AutoDowngradeConfigResponse,
    BudgetSummaryResponse,
    SeedSpentRequest,
    SeedSpentResponse,
    SetBudgetRequest,
    TeamBudgetAllocation,
    UserAppBudgetsResponse,
)

router = APIRouter(prefix="/admin/budgets", tags=["Budget Management"])


@router.put("/team/{team_id}")
async def set_team_budget(
    request: Request,
    team_id: str,
    body: SetBudgetRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.budget_service import BudgetService

    svc: BudgetService = request.app.state.budget_service
    await svc.set_team_budget(
        session,
        team_id=uuid.UUID(team_id),
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
    return {"status": "ok"}


@router.put("/user/{user_id}")
async def set_user_budget(
    request: Request,
    user_id: str,
    body: SetBudgetRequest,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    await svc.set_user_budget(
        session,
        user_id=uuid.UUID(user_id),
        data=body,
        actor=user,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
    return {"status": "ok"}


@router.put("/spent", response_model=SeedSpentResponse)
async def seed_budget_spent(
    request: Request,
    body: SeedSpentRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """Batch-inject absolute spent (USD) per scope for migration burn-rate continuation.

    Overwrites used_usd in DB + Redis. Idempotent. Per-item partial failure reported.
    """
    from app.services.budget_service import BudgetService

    svc: BudgetService = request.app.state.budget_service
    return await svc.seed_spent(
        session,
        items=body.items,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.delete("/user/{user_id}", status_code=204)
async def delete_user_budget(
    request: Request,
    user_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """개인 예산 삭제 → 팀 예산 적용으로 전환."""
    svc: BudgetService = request.app.state.budget_service
    await svc.delete_user_budget(
        session,
        user_id=uuid.UUID(user_id),
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.put("/user/{user_id}/app/{client}")
async def set_user_client_budget(
    request: Request,
    user_id: str,
    client: str,
    body: SetBudgetRequest,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """Set a per-app (client) budget for a user."""
    from app.services.budget_service import BudgetService
    from fastapi import HTTPException

    svc: BudgetService = request.app.state.budget_service
    try:
        await svc.set_user_client_budget(
            session,
            user_id=uuid.UUID(user_id),
            client=client,
            data=body,
            actor=user,
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=request.headers.get("x-request-id", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok"}


@router.delete("/user/{user_id}/app/{client}", status_code=204)
async def clear_user_client_budget(
    request: Request,
    user_id: str,
    client: str,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """Clear (deactivate) the per-app budget for a user."""
    from app.services.budget_service import BudgetService
    from fastapi import HTTPException

    svc: BudgetService = request.app.state.budget_service
    try:
        await svc.clear_user_client_budget(
            session,
            user_id=uuid.UUID(user_id),
            client=client,
            actor=user,
            ip_address=request.client.host if request.client else "0.0.0.0",
            request_id=request.headers.get("x-request-id", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/user/{user_id}/apps", response_model=UserAppBudgetsResponse)
async def get_user_app_budgets(
    request: Request,
    user_id: str,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    """Read active per-app (client) budgets for a user, for UI prefill."""
    from app.services.budget_service import BudgetService

    svc: BudgetService = request.app.state.budget_service
    apps = await svc.get_user_app_budgets(session, user_id=uuid.UUID(user_id), actor=user)
    return UserAppBudgetsResponse(user_id=str(user_id), apps=apps)


@router.get("/team/{team_id}/allocation", response_model=TeamBudgetAllocation | None)
async def get_team_allocation(
    request: Request,
    team_id: str,
    period: str | None = Query(None, description="YYYY-MM (defaults to current month)"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    from datetime import date as _date

    from app.services.budget_service import BudgetService

    svc: BudgetService = request.app.state.budget_service
    effective_period = period or _date.today().strftime("%Y-%m")
    return await svc.get_team_allocation(
        session,
        team_id=uuid.UUID(team_id),
        period=effective_period,
    )


@router.put("/team/{team_id}/allocate")
async def allocate_team_budget(
    request: Request,
    team_id: str,
    body: AllocateBudgetRequest,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    await svc.allocate_team_budget(
        session,
        team_id=uuid.UUID(team_id),
        data=body,
        actor=user,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
    return {"status": "ok"}


@router.get("/summary", response_model=BudgetSummaryResponse)
async def get_budget_summary(
    request: Request,
    scope: str | None = None,
    target_id: str | None = None,
    period: str = Query(description="YYYY-MM"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    redis = request.app.state.redis
    return await svc.get_budget_summary(
        session,
        redis=redis,
        scope=scope,
        target_id=uuid.UUID(target_id) if target_id else None,
        period=period,
    )


@router.get("/{scope}/{scope_id}/downgrade", response_model=AutoDowngradeConfigResponse)
async def get_downgrade_config(
    request: Request,
    scope: str,
    scope_id: str,
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    return await svc.get_downgrade_config(
        session,
        scope=BudgetScope(scope.upper()),
        scope_id=uuid.UUID(scope_id),
    )


@router.put("/{scope}/{scope_id}/downgrade", response_model=AutoDowngradeConfigResponse)
async def set_downgrade_config(
    request: Request,
    scope: str,
    scope_id: str,
    body: AutoDowngradeConfigRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    return await svc.set_downgrade_config(
        session,
        scope=BudgetScope(scope.upper()),
        scope_id=uuid.UUID(scope_id),
        data=body,
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )


@router.delete("/{scope}/{scope_id}/downgrade", status_code=204)
async def delete_downgrade_config(
    request: Request,
    scope: str,
    scope_id: str,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    svc: BudgetService = request.app.state.budget_service
    await svc.delete_downgrade_config(
        session,
        scope=BudgetScope(scope.upper()),
        scope_id=uuid.UUID(scope_id),
        actor=admin,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )
