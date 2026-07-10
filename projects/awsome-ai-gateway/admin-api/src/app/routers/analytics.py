# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin, require_admin_or_team_leader
from app.core.db import get_db_session
from app.core.usage_filters import cost_period_filter, kst_month_expr
from app.models.usage import UsageLog

router = APIRouter(prefix="/admin/analytics", tags=["Analytics"])


@router.get("")
async def get_analytics(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    group_by: str = Query("model", description="model | team | department | user"),
    scope: str = Query("all", description="all | team:{uuid}"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    from app.services.analytics_service import AnalyticsService

    svc: AnalyticsService = request.app.state.analytics_service
    return await svc.get_analytics(
        session,
        period=period,
        group_by=group_by,
        scope=scope,
        actor=user,
    )


@router.get("/models")
async def get_model_cost_analytics(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM format"),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    now = date.today()
    if not period:
        period = f"{now.year}-{now.month:02d}"

    model_stmt = (
        select(
            UsageLog.model_alias,
            func.count().label("request_count"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost_usd"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label("cache_creation_tokens"),
            func.avg(UsageLog.latency_ms).label("avg_latency_ms"),
        )
        .where(cost_period_filter(period))  # §59 SUCCESS + KST
        .group_by(UsageLog.model_alias)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    model_result = await session.execute(model_stmt)

    models = []
    for row in model_result.all():
        total_tokens = (row.input_tokens or 0) + (row.output_tokens or 0)
        cost_per_1k = (float(row.total_cost_usd) / total_tokens * 1000) if total_tokens > 0 else 0
        models.append({
            "model_alias": row.model_alias,
            "request_count": row.request_count,
            "total_cost_usd": round(float(row.total_cost_usd), 4),
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_creation_tokens": row.cache_creation_tokens,
            "avg_latency_ms": round(float(row.avg_latency_ms or 0)),
            "cost_per_1k_tokens": round(cost_per_1k, 6),
        })

    # 일별 binning 도 KST(§59) — func.date(timestamptz)는 세션 TZ(UTC)라 KST 변환 후 date.
    _kst_day = func.date(func.timezone("Asia/Seoul", UsageLog.requested_at))
    daily_stmt = (
        select(
            _kst_day.label("day"),
            UsageLog.model_alias,
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
        )
        .where(cost_period_filter(period))  # §59 SUCCESS + KST
        .group_by(_kst_day, UsageLog.model_alias)
        .order_by(_kst_day)
    )
    daily_result = await session.execute(daily_stmt)
    daily_trend = [
        {
            "date": str(row.day),
            "model_alias": row.model_alias,
            "cost_usd": round(float(row.cost_usd), 4),
        }
        for row in daily_result.all()
    ]

    grand_total = sum(m["total_cost_usd"] for m in models)
    return {
        "period": period,
        "total_cost_usd": round(grand_total, 4),
        "models": models,
        "daily_trend": daily_trend,
    }


@router.get("/export")
async def export_analytics(
    request: Request,
    format: str = Query("csv", description="csv | json"),
    period: str = Query(description="YYYY-MM format"),
    group_by: str = Query("model"),
    user: CurrentUser = Depends(require_admin_or_team_leader),
    session: AsyncSession = Depends(get_db_session),
):
    svc: AnalyticsService = request.app.state.analytics_service
    content, content_type = await svc.export_analytics(
        session,
        format=format,
        period=period,
        group_by=group_by,
        actor=user,
    )

    filename = f"analytics_{period}.{format}"
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/usage/by-user-model")
async def get_usage_by_user_model(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    date: str = Query(description="YYYY-MM-DD format (within period month)"),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """User × Model 누적 (period 1일 ~ date, KST, SUCCESS only). Dashboard 메인 테이블용."""
    from app.services.analytics_service import AnalyticsService

    svc: AnalyticsService = request.app.state.analytics_service
    return await svc.get_usage_by_user_model(session, period=period, date=date)


@router.get("/usage/by-user")
async def get_usage_by_user(
    request: Request,
    period: str = Query(description="YYYY-MM format"),
    date: str = Query(description="YYYY-MM-DD format (within period month)"),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """User 단위 누적 (period 1일 ~ date, KST, SUCCESS only). Dashboard 요약 테이블용."""
    from app.services.analytics_service import AnalyticsService

    svc: AnalyticsService = request.app.state.analytics_service
    return await svc.get_usage_by_user(session, period=period, date=date)
