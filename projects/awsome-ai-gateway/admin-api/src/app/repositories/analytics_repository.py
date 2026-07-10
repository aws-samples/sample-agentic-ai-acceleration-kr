# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.usage_filters import cost_period_filter, kst_month_expr
from app.models.usage import ROIAggregation, ROIScope, UsageLog


class AnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── ROIAggregation (pre-aggregated read) ──

    async def get_aggregations(
        self,
        period: str,
        scope: ROIScope | None = None,
        scope_id: uuid.UUID | None = None,
    ) -> list[ROIAggregation]:
        stmt = select(ROIAggregation).where(ROIAggregation.period == period)
        if scope:
            stmt = stmt.where(ROIAggregation.scope == scope)
        if scope_id:
            stmt = stmt.where(ROIAggregation.scope_id == scope_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def upsert_aggregation(self, agg: ROIAggregation) -> ROIAggregation:
        """UPSERT by period + scope + scope_id."""
        existing_stmt = select(ROIAggregation).where(
            ROIAggregation.period == agg.period,
            ROIAggregation.scope == agg.scope,
            ROIAggregation.scope_id == agg.scope_id,
        )
        result = await self._session.execute(existing_stmt)
        existing = result.scalar_one_or_none()

        if existing:
            existing.total_cost_usd = agg.total_cost_usd
            existing.cost_per_user_usd = agg.cost_per_user_usd
            existing.budget_utilization_pct = agg.budget_utilization_pct
            existing.cost_by_model = agg.cost_by_model
            existing.active_users = agg.active_users
            existing.active_user_rate_pct = agg.active_user_rate_pct
            existing.requests_per_user_per_day = agg.requests_per_user_per_day
            existing.activation_gap_pct = agg.activation_gap_pct
            existing.aggregated_at = agg.aggregated_at
            existing.aggregated_by = agg.aggregated_by
            return existing
        else:
            self._session.add(agg)
            await self._session.flush()
            return agg

    # ── UsageLog queries (for scheduler aggregation) ──

    async def sum_usage_by_model(
        self, period: str, scope: ROIScope, scope_id: uuid.UUID | None
    ) -> dict[str, Decimal]:
        """Returns {model_alias: total_cost_usd} for the given period/scope."""
        stmt = select(
            UsageLog.model_alias,
            func.sum(UsageLog.cost_usd).label("total_cost"),
        ).where(
            cost_period_filter(period),  # §59 SUCCESS + KST
        )
        stmt = self._apply_scope_filter(stmt, scope, scope_id)
        stmt = stmt.group_by(UsageLog.model_alias)
        result = await self._session.execute(stmt)
        return {row.model_alias: row.total_cost or Decimal("0") for row in result}

    async def count_active_users(self, period: str, scope: ROIScope, scope_id: uuid.UUID | None) -> int:
        stmt = select(func.count(distinct(UsageLog.user_id))).where(
            cost_period_filter(period),  # §59 SUCCESS + KST
        )
        stmt = self._apply_scope_filter(stmt, scope, scope_id)
        result = await self._session.execute(stmt)
        return result.scalar_one() or 0

    async def total_requests(self, period: str, scope: ROIScope, scope_id: uuid.UUID | None) -> int:
        stmt = select(func.count(UsageLog.id)).where(
            cost_period_filter(period),  # §59 SUCCESS + KST
        )
        stmt = self._apply_scope_filter(stmt, scope, scope_id)
        result = await self._session.execute(stmt)
        return result.scalar_one() or 0

    async def total_tokens(self, period: str, scope: ROIScope, scope_id: uuid.UUID | None) -> int:
        stmt = select(
            func.coalesce(func.sum(UsageLog.input_tokens), 0)
            + func.coalesce(func.sum(UsageLog.output_tokens), 0)
        ).where(
            cost_period_filter(period),  # §59 SUCCESS + KST
        )
        stmt = self._apply_scope_filter(stmt, scope, scope_id)
        result = await self._session.execute(stmt)
        return result.scalar_one() or 0

    @staticmethod
    def _apply_scope_filter(stmt, scope: ROIScope, scope_id: uuid.UUID | None):
        if scope == ROIScope.USER and scope_id:
            stmt = stmt.where(UsageLog.user_id == scope_id)
        elif scope == ROIScope.TEAM and scope_id:
            stmt = stmt.where(UsageLog.team_id == scope_id)
        elif scope == ROIScope.DEPT and scope_id:
            stmt = stmt.where(UsageLog.dept_id == scope_id)
        # GLOBAL: no filter
        return stmt
