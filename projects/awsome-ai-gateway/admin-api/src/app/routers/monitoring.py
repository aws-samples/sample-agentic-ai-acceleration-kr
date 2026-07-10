# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.config import get_settings
from app.core.db import get_db_session
from app.models.auth import User
from app.models.model import ModelAlias, ModelStatus
from app.models.usage import UsageLog, UsageStatus

router = APIRouter(prefix="/admin/monitoring", tags=["Monitoring"])


@router.get("/overview")
async def monitoring_overview(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)

    stmt = select(
        func.count().label("total_requests"),
        func.count().filter(UsageLog.status == UsageStatus.ERROR).label("error_count"),
        func.avg(UsageLog.latency_ms).label("avg_latency_ms"),
        func.percentile_cont(0.95).within_group(UsageLog.latency_ms).label("p95_latency_ms"),
        func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost_usd"),
    ).where(UsageLog.requested_at >= one_hour_ago)
    result = await session.execute(stmt)
    row = result.one()

    model_count_stmt = select(func.count()).where(ModelAlias.status == ModelStatus.ACTIVE)
    model_count = (await session.execute(model_count_stmt)).scalar_one()

    total = row.total_requests or 0
    errors = row.error_count or 0
    error_rate = (errors / total * 100) if total > 0 else 0

    return {
        "timestamp": now.isoformat(),
        "active_models": model_count,
        "last_1h": {
            "total_requests": total,
            "error_count": errors,
            "error_rate_pct": round(error_rate, 2),
            "avg_latency_ms": round(float(row.avg_latency_ms or 0)),
            "p95_latency_ms": round(float(row.p95_latency_ms or 0)),
            "total_cost_usd": round(float(row.total_cost_usd or 0), 4),
        },
    }


@router.get("/models")
async def monitoring_models(
    request: Request,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)

    stmt = (
        select(
            UsageLog.model_alias,
            func.count().label("requests"),
            func.avg(UsageLog.latency_ms).label("avg_latency_ms"),
            func.count().filter(UsageLog.status == UsageStatus.ERROR).label("error_count"),
            func.max(UsageLog.requested_at).label("last_request_at"),
        )
        .where(UsageLog.requested_at >= one_hour_ago)
        .group_by(UsageLog.model_alias)
        .order_by(func.count().desc())
    )
    result = await session.execute(stmt)

    models = []
    for row in result.all():
        total = row.requests or 0
        errors = row.error_count or 0
        error_rate = (errors / total * 100) if total > 0 else 0
        models.append({
            "alias": row.model_alias,
            "status": "ACTIVE",
            "last_1h_requests": total,
            "avg_latency_ms": round(float(row.avg_latency_ms or 0)),
            "error_rate_pct": round(error_rate, 2),
            "last_request_at": row.last_request_at.isoformat() if row.last_request_at else None,
        })

    return {"models": models}


@router.get("/events")
async def monitoring_events(
    request: Request,
    limit: int = Query(default=50, le=200),
    event_type: str = Query(
        default="all",
        regex="^(all|success|error|timeout|slow|abnormal)$",
        description="all=전체, success=정상, error=에러, timeout=타임아웃, slow=첫 응답(TTFT) 3s 초과, abnormal=error+timeout+slow",
    ),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    settings = get_settings()
    slow_ms = settings.TTFT_SLOW_MS

    now = datetime.now(timezone.utc)
    one_day_ago = now - timedelta(days=1)

    # TTFT 우선, 없으면(구버전 데이터) latency_ms 로 폴백해 지연 판정.
    _effective = func.coalesce(UsageLog.ttft_ms, UsageLog.latency_ms)

    stmt = (
        select(
            UsageLog.requested_at,
            UsageLog.user_id,
            UsageLog.model_alias,
            UsageLog.status,
            UsageLog.latency_ms,
            UsageLog.ttft_ms,
            UsageLog.cost_usd,
            UsageLog.downgraded_from,
        )
        .where(UsageLog.requested_at >= one_day_ago)
    )

    if event_type == "abnormal":
        stmt = stmt.where(
            (UsageLog.status.in_([UsageStatus.ERROR, UsageStatus.TIMEOUT]))
            | (_effective > slow_ms)
        )
    elif event_type == "error":
        stmt = stmt.where(UsageLog.status == UsageStatus.ERROR)
    elif event_type == "timeout":
        stmt = stmt.where(UsageLog.status == UsageStatus.TIMEOUT)
    elif event_type == "slow":
        stmt = stmt.where(
            UsageLog.status == UsageStatus.SUCCESS,
            _effective > slow_ms,
        )
    elif event_type == "success":
        stmt = stmt.where(
            UsageLog.status == UsageStatus.SUCCESS,
            _effective <= slow_ms,
        )
    # event_type == "all": 추가 필터 없음

    stmt = stmt.order_by(UsageLog.requested_at.desc()).limit(limit)
    result = await session.execute(stmt)

    events = []
    for row in result.all():
        # event_type 결정: ERROR/TIMEOUT 우선, 그 외엔 TTFT(없으면 latency) 로 SLOW vs SUCCESS
        effective = row.ttft_ms if row.ttft_ms is not None else row.latency_ms
        if row.status == UsageStatus.ERROR:
            ev_type = "ERROR"
        elif row.status == UsageStatus.TIMEOUT:
            ev_type = "TIMEOUT"
        elif (effective or 0) > slow_ms:
            ev_type = "SLOW_REQUEST"
        else:
            ev_type = "SUCCESS"

        ttft_str = f"{row.ttft_ms}ms" if row.ttft_ms is not None else "-"
        events.append({
            "timestamp": row.requested_at.isoformat(),
            "user_id": str(row.user_id),
            "model_alias": row.model_alias,
            "downgraded_from": row.downgraded_from,
            "event_type": ev_type,
            "ttft_ms": row.ttft_ms,
            "latency_ms": row.latency_ms,
            "detail": f"ttft={ttft_str}, total={row.latency_ms}ms, cost=${float(row.cost_usd):.4f}",
        })

    return {"events": events}


@router.get("/users")
async def monitoring_users(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)

    stmt = (
        select(
            UsageLog.user_id,
            User.email,
            User.display_name,
            func.count().label("requests"),
            func.coalesce(func.sum(UsageLog.input_tokens + UsageLog.output_tokens), 0).label("tokens"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
            func.count().filter(UsageLog.status == UsageStatus.ERROR).label("error_count"),
            func.max(UsageLog.requested_at).label("last_request_at"),
        )
        .join(User, User.id == UsageLog.user_id)
        .where(UsageLog.requested_at >= one_hour_ago)
        .group_by(UsageLog.user_id, User.email, User.display_name)
        .order_by(func.sum(UsageLog.cost_usd).desc().nullslast())
        .limit(limit)
    )
    result = await session.execute(stmt)

    users = []
    for row in result.all():
        requests = row.requests or 0
        errors = row.error_count or 0
        error_rate = (errors / requests * 100) if requests > 0 else 0
        users.append({
            "user_id": str(row.user_id),
            "email": row.email,
            "display_name": row.display_name,
            "requests": requests,
            "tokens": int(row.tokens or 0),
            "cost_usd": round(float(row.cost_usd or 0), 4),
            "error_rate_pct": round(error_rate, 2),
            "last_request_at": row.last_request_at.isoformat() if row.last_request_at else None,
        })

    return {"users": users}
