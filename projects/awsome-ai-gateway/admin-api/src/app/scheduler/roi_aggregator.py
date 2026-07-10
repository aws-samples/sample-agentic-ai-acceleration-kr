# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usage import ROIAggregation, ROIScope
from app.repositories.analytics_repository import AnalyticsRepository

logger = structlog.get_logger()


async def aggregate_usage(session: AsyncSession, period: str) -> None:
    """Aggregate UsageLog into ROIAggregation for the given period.

    Runs per-scope: GLOBAL, then per-TEAM, per-USER, per-DEPT.
    MVP: cost metrics only. Productivity metrics = null.
    """
    repo = AnalyticsRepository(session)
    run_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc)

    logger.info("roi_aggregator.start", period=period, run_id=run_id)

    # ── GLOBAL scope ──
    cost_by_model = await repo.sum_usage_by_model(period, ROIScope.GLOBAL, None)
    total_cost = sum(cost_by_model.values(), Decimal("0"))
    active_users = await repo.count_active_users(period, ROIScope.GLOBAL, None)

    avg_cost = total_cost / active_users if active_users > 0 else Decimal("0")

    # ── Productivity metrics (from productivity_events & git_events) ──
    prod_metrics = await _aggregate_productivity(session, period)

    global_agg = ROIAggregation(
        id=uuid.uuid4(),
        period=period,
        scope=ROIScope.GLOBAL,
        scope_id=None,
        total_cost_usd=total_cost,
        cost_per_user_usd=avg_cost,
        budget_utilization_pct=Decimal("0"),
        cost_by_model={k: float(v) for k, v in cost_by_model.items()},
        active_users=active_users,
        active_user_rate_pct=Decimal("0"),
        requests_per_user_per_day=Decimal("0"),
        activation_gap_pct=Decimal("0"),
        code_acceptance_rate_pct=prod_metrics["acceptance_rate"],
        cost_per_accepted_code_usd=prod_metrics["cost_per_accepted"],
        generated_lines_per_session=prod_metrics["lines_per_session"],
        roi_index=prod_metrics["roi_index"],
        aggregated_at=now,
        aggregated_by=run_id,
    )
    await repo.upsert_aggregation(global_agg)

    # ── Per-TEAM scope ──
    # Get distinct team_ids from usage logs
    from sqlalchemy import distinct, select
    from app.models.usage import UsageLog
    from sqlalchemy import func

    # 팀 탐색은 KST 월 경계(§59)로 — 비용 집계와 같은 창의 팀을 찾는다. status 필터는
    # 안 함(에러만 있는 팀도 ROI 집계 대상에 포함되도록).
    from app.core.usage_filters import kst_month_expr
    stmt = select(distinct(UsageLog.team_id)).where(
        kst_month_expr() == period
    )
    result = await session.execute(stmt)
    team_ids = [row[0] for row in result]

    for team_id in team_ids:
        team_cost_by_model = await repo.sum_usage_by_model(period, ROIScope.TEAM, team_id)
        team_total_cost = sum(team_cost_by_model.values(), Decimal("0"))
        team_active = await repo.count_active_users(period, ROIScope.TEAM, team_id)
        team_avg_cost = team_total_cost / team_active if team_active > 0 else Decimal("0")

        team_agg = ROIAggregation(
            id=uuid.uuid4(),
            period=period,
            scope=ROIScope.TEAM,
            scope_id=team_id,
            total_cost_usd=team_total_cost,
            cost_per_user_usd=team_avg_cost,
            budget_utilization_pct=Decimal("0"),
            cost_by_model={k: float(v) for k, v in team_cost_by_model.items()},
            active_users=team_active,
            active_user_rate_pct=Decimal("0"),
            requests_per_user_per_day=Decimal("0"),
            activation_gap_pct=Decimal("0"),
            aggregated_at=now,
            aggregated_by=run_id,
        )
        await repo.upsert_aggregation(team_agg)

    await session.commit()
    logger.info("roi_aggregator.complete", period=period, run_id=run_id, teams=len(team_ids))


async def _aggregate_productivity(session: AsyncSession, period: str) -> dict:
    from sqlalchemy import distinct, func, select
    from app.models.usage import (
        GitEvent, GitEventType,
        ProductivityEvent, ProductivityEventType,
        UsageLog,
    )

    # Lines generated / accepted
    lines_stmt = select(
        func.coalesce(func.sum(ProductivityEvent.lines_generated), 0).label("gen"),
        func.coalesce(func.sum(ProductivityEvent.lines_accepted), 0).label("acc"),
        func.count(distinct(ProductivityEvent.session_id)).label("sessions"),
        func.count().filter(ProductivityEvent.event_type == ProductivityEventType.CODE_GENERATED).label("gen_events"),
        func.count().filter(ProductivityEvent.event_type == ProductivityEventType.CODE_ACCEPTED).label("acc_events"),
    ).where(func.to_char(ProductivityEvent.created_at, "YYYY-MM") == period)
    row = (await session.execute(lines_stmt)).one()

    lines_gen = int(row.gen or 0)
    lines_acc = int(row.acc or 0)
    sessions = int(row.sessions or 0)
    gen_events = int(row.gen_events or 0)
    acc_events = int(row.acc_events or 0)

    acceptance_rate = Decimal(str(round(acc_events / gen_events * 100, 2))) if gen_events > 0 else None
    lines_per_session = Decimal(str(round(lines_gen / sessions, 2))) if sessions > 0 else None

    # Total cost for ROI — §59 비용 집계 표준(SUCCESS + KST).
    from app.core.usage_filters import cost_period_filter
    cost_stmt = select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
        cost_period_filter(period)
    )
    total_cost = (await session.execute(cost_stmt)).scalar_one() or Decimal("0")

    cost_per_accepted = Decimal(str(round(float(total_cost) / lines_acc, 4))) if lines_acc > 0 else None

    # ROI index = accepted_lines / cost (higher = better)
    roi_index = Decimal(str(round(lines_acc / float(total_cost), 4))) if float(total_cost) > 0 and lines_acc > 0 else None

    return {
        "acceptance_rate": acceptance_rate,
        "cost_per_accepted": cost_per_accepted,
        "lines_per_session": lines_per_session,
        "roi_index": roi_index,
    }
