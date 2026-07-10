# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import csv
import io
import re
import uuid
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError, ValidationError
from app.models.auth import UserRole
from app.models.usage import ROIScope
from app.repositories.analytics_repository import AnalyticsRepository
from app.schemas.analytics import (
    AnalyticsResponse,
    CostSummary,
    ModelBreakdown,
    TeamBreakdown,
    TrendItem,
    UsageByUserItem,
    UsageByUserModelItem,
    UsageByUserModelResponse,
    UsageByUserResponse,
    UserBreakdown,
)

logger = structlog.get_logger()

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_period_date(period: str, date: str) -> None:
    """period=YYYY-MM, date=YYYY-MM-DD, and date must be within period's month."""
    if not _PERIOD_RE.match(period):
        raise ValidationError(f"Invalid period format: {period}. Expected YYYY-MM")
    if not _DATE_RE.match(date):
        raise ValidationError(f"Invalid date format: {date}. Expected YYYY-MM-DD")
    # Calendar validity (Codex review): the regex accepts 2026-06-31 etc., which
    # then fails at the PostgreSQL date cast with an opaque 500. Reject up front.
    import datetime as _dt
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        raise ValidationError(f"Invalid calendar date: {date}")
    if date[:7] != period:
        raise ValidationError(f"date {date} must fall within period {period}")


class AnalyticsService:
    async def get_analytics(
        self,
        session: AsyncSession,
        *,
        period: str,
        group_by: str = "model",
        scope: str = "all",
        actor: CurrentUser,
    ) -> AnalyticsResponse:
        repo = AnalyticsRepository(session)

        # Determine scope filter
        roi_scope: ROIScope | None = None
        scope_id: uuid.UUID | None = None

        if scope.startswith("team:"):
            team_id = uuid.UUID(scope.split(":")[1])
            # TEAM_LEADER can only see own team
            if actor.role == UserRole.TEAM_LEADER and actor.team_id != team_id:
                raise ForbiddenError("Team leaders can only view analytics for their own team")
            roi_scope = ROIScope.TEAM
            scope_id = team_id
        elif scope == "all":
            # TEAM_LEADER restricted to own team
            if actor.role == UserRole.TEAM_LEADER:
                roi_scope = ROIScope.TEAM
                scope_id = actor.team_id
        # ADMIN: no restriction

        # Real-time aggregation from usage_logs (not pre-aggregated roi_aggregations)
        query_scope = roi_scope or ROIScope.GLOBAL

        cost_by_model = await repo.sum_usage_by_model(period, query_scope, scope_id)
        total_cost = sum(cost_by_model.values(), Decimal("0"))
        active_users_count = await repo.count_active_users(period, query_scope, scope_id)
        total_requests_count = await repo.total_requests(period, query_scope, scope_id)
        total_tokens_count = await repo.total_tokens(period, query_scope, scope_id)

        avg_cost = total_cost / active_users_count if active_users_count > 0 else Decimal("0")

        cost_summary = CostSummary(
            total_requests=total_requests_count,
            total_tokens=total_tokens_count,
            total_cost_usd=total_cost,
            active_users=active_users_count,
            avg_cost_per_user_usd=avg_cost,
        )

        by_model = [
            ModelBreakdown(model=model, cost_usd=cost)
            for model, cost in cost_by_model.items()
        ]

        # Team breakdown — aggregate per team from usage_logs
        by_team: list[TeamBreakdown] = []
        if not roi_scope or roi_scope == ROIScope.GLOBAL:
            team_costs = await repo.sum_usage_by_model(period, ROIScope.GLOBAL, None)
            # Get per-team costs
            from sqlalchemy import distinct, func, select
            from app.models.usage import UsageLog
            from app.core.usage_filters import cost_period_filter
            stmt = select(
                UsageLog.team_id,
                func.sum(UsageLog.cost_usd).label("cost"),
                func.count(distinct(UsageLog.user_id)).label("users"),
            ).where(
                cost_period_filter(period),  # §59 SUCCESS + KST (team 귀속은 usage_logs.team_id 직접)
            ).group_by(UsageLog.team_id)
            result = await session.execute(stmt)
            for row in result:
                if row.team_id:
                    by_team.append(TeamBreakdown(
                        team=str(row.team_id),
                        team_id=str(row.team_id),
                        cost_usd=row.cost or Decimal("0"),
                        active_users=row.users or 0,
                    ))

        # User breakdown — group_by='user' 요청 시만 집계(불필요 조인 회피). §60.9:
        # 그간 UI 에 '사용자별' 옵션은 있었으나 백엔드가 group_by 무시 → by_model 표시되던
        # 버그 수정. usage_logs SUCCESS+KST(cost_period_filter) + User 조인, PII(sso_subject)
        # 미노출(display_name·email 만). 상위 50명(차트 가독).
        by_user: list[UserBreakdown] = []
        if group_by == "user":
            from sqlalchemy import func, select
            from app.models.usage import UsageLog
            from app.models.auth import User
            from app.core.usage_filters import cost_period_filter

            user_where = [cost_period_filter(period)]
            if scope_id is not None:  # TEAM_LEADER/team scope 격리
                user_where.append(UsageLog.team_id == scope_id)
            ustmt = (
                select(
                    User.display_name.label("name"),
                    User.email.label("email"),
                    func.sum(UsageLog.cost_usd).label("cost"),
                    func.count().label("requests"),
                )
                .join(User, User.id == UsageLog.user_id)
                .where(*user_where)
                .group_by(User.id, User.display_name, User.email)
                .order_by(func.sum(UsageLog.cost_usd).desc())
                .limit(50)
            )
            for row in (await session.execute(ustmt)).all():
                by_user.append(UserBreakdown(
                    user=row.name,
                    email=row.email,
                    cost_usd=row.cost or Decimal("0"),
                    requests=row.requests or 0,
                ))

        return AnalyticsResponse(
            period=period,
            cost_summary=cost_summary,
            by_model=by_model,
            by_team=by_team,
            by_user=by_user,
        )

    async def export_analytics(
        self,
        session: AsyncSession,
        *,
        format: str,
        period: str,
        group_by: str,
        actor: CurrentUser,
    ) -> tuple[str, str]:
        """Returns (content, content_type)."""
        response = await self.get_analytics(
            session, period=period, group_by=group_by, scope="all", actor=actor
        )

        if format == "csv":
            return self._to_csv(response), "text/csv"
        else:
            return response.model_dump_json(indent=2), "application/json"

    async def get_usage_by_user_model(
        self,
        session: AsyncSession,
        *,
        period: str,
        date: str,
    ) -> UsageByUserModelResponse:
        """User × Model 누적 (period 1일 ~ date, KST, SUCCESS only)."""
        from sqlalchemy import func, select

        from app.models.auth import Department, Team, User
        from app.models.usage import UsageLog, UsageStatus

        _validate_period_date(period, date)

        period_start = f"{period}-01"
        kst_day = func.date(func.timezone("Asia/Seoul", UsageLog.requested_at))

        stmt = (
            select(
                UsageLog.user_id.label("user_id"),
                User.display_name.label("user_name"),
                UsageLog.model_alias.label("model_alias"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
                func.count().label("calls"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("cache_read_tokens"),
                func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label(
                    "cache_creation_tokens"
                ),
                func.avg(UsageLog.latency_ms).label("avg_latency_ms"),
                Team.id.label("team_id"),
                Team.name.label("team_name"),
                Department.id.label("department_id"),
                Department.name.label("department_name"),
            )
            .select_from(UsageLog)
            .outerjoin(User, User.id == UsageLog.user_id)
            .outerjoin(Team, Team.id == User.team_id)
            .outerjoin(Department, Department.id == Team.dept_id)
            .where(
                UsageLog.status == UsageStatus.SUCCESS,
                # cast string bounds to date — kst_day is a DATE, comparing against a
                # bare str raises asyncpg "operator does not exist: date >= varchar".
                kst_day >= func.date(period_start),
                kst_day <= func.date(date),
            )
            .group_by(
                UsageLog.user_id,
                User.display_name,
                UsageLog.model_alias,
                Team.id,
                Team.name,
                Department.id,
                Department.name,
            )
            .order_by(func.sum(UsageLog.cost_usd).desc())
        )

        rows = (await session.execute(stmt)).all()

        items = [
            UsageByUserModelItem(
                date=date,
                user_id=str(r.user_id),
                user_name=r.user_name,
                model_alias=r.model_alias,
                cost_usd=r.cost_usd,
                calls=r.calls,
                input_tokens=r.input_tokens or 0,
                output_tokens=r.output_tokens or 0,
                cache_read_tokens=r.cache_read_tokens or 0,
                cache_write_tokens=r.cache_creation_tokens or 0,
                department_id=str(r.department_id) if r.department_id else None,
                department_name=r.department_name,
                team_id=str(r.team_id) if r.team_id else None,
                team_name=r.team_name,
                avg_latency_ms=round(float(r.avg_latency_ms or 0)),
            )
            for r in rows
        ]
        return UsageByUserModelResponse(period=period, date=date, items=items)

    async def get_usage_by_user(
        self,
        session: AsyncSession,
        *,
        period: str,
        date: str,
    ) -> UsageByUserResponse:
        """User 단위 누적 (period 1일 ~ date, KST, SUCCESS only). Dashboard 요약 테이블용."""
        from sqlalchemy import func, select

        from app.models.auth import Department, Team, User
        from app.models.usage import UsageLog, UsageStatus

        _validate_period_date(period, date)

        period_start = f"{period}-01"
        kst_day = func.date(func.timezone("Asia/Seoul", UsageLog.requested_at))

        stmt = (
            select(
                UsageLog.user_id.label("user_id"),
                User.display_name.label("user_name"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
                func.count().label("calls"),
                Team.id.label("team_id"),
                Team.name.label("team_name"),
                Department.id.label("department_id"),
                Department.name.label("department_name"),
            )
            .select_from(UsageLog)
            .outerjoin(User, User.id == UsageLog.user_id)
            .outerjoin(Team, Team.id == User.team_id)
            .outerjoin(Department, Department.id == Team.dept_id)
            .where(
                UsageLog.status == UsageStatus.SUCCESS,
                # cast string bounds to date — kst_day is a DATE, comparing against a
                # bare str raises asyncpg "operator does not exist: date >= varchar".
                kst_day >= func.date(period_start),
                kst_day <= func.date(date),
            )
            .group_by(
                UsageLog.user_id,
                User.display_name,
                Team.id,
                Team.name,
                Department.id,
                Department.name,
            )
            .order_by(func.sum(UsageLog.cost_usd).desc())
        )

        rows = (await session.execute(stmt)).all()

        items = [
            UsageByUserItem(
                date=date,
                user_id=str(r.user_id),
                user_name=r.user_name,
                cost_usd=r.cost_usd,
                calls=r.calls,
                department_id=str(r.department_id) if r.department_id else None,
                department_name=r.department_name,
                team_id=str(r.team_id) if r.team_id else None,
                team_name=r.team_name,
            )
            for r in rows
        ]
        return UsageByUserResponse(period=period, date=date, items=items)

    @staticmethod
    def _to_csv(data: AnalyticsResponse) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["period", "model", "cost_usd"])
        for item in data.by_model:
            writer.writerow([data.period, item.model, str(item.cost_usd)])
        return output.getvalue()
