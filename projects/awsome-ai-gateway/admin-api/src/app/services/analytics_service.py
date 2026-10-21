# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import datetime as _dt
import re
import uuid
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.exceptions import ForbiddenError, ValidationError
from app.core.usage_filters import (
    cost_day_range_filter,
    cost_period_filter,
    day_range_to_utc,
    period_to_utc_range,
)
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

#: 직접 입력 범위의 최대 길이(일, 양끝 포함). 캐시되지 않는 요청 하나가 usage_logs 전체 집계를
#: 6~9번 돌리므로(요약·모델·팀·사용자·추이·seed), 상한이 없으면 2000-01-01~2100-12-31 같은 범위가
#: 그대로 전 테이블 스캔이 된다. 93일 = 연속한 석 달(분기)을 담는 가장 짧은 길이.
MAX_ANALYTICS_RANGE_DAYS = 93

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


def _resolve_date_range(start_date: str | None, end_date: str | None) -> tuple[str, str] | None:
    """(start_date, end_date) 를 검증해 반환. 둘 다 없으면 None(= period 월 전체).

    왜 "조용히 무시" 가 아니라 예외(400)인가: `직접 입력` 날짜 선택기는 이 두 값을 계속
    보내왔지만 라우트가 선언하지 않아 FastAPI 가 422 없이 버렸다 — 5일 범위를 고른 운영자가
    한 달 전체 비용을 5일치로 읽었다. 이제 범위를 실제로 반영하고, 반영할 수 없는 입력
    (한쪽만 / 역순 / 형식·달력 오류)은 **소리 내어** 거절한다. 그럴듯한 오답보다 에러가 낫다.
    형식·달력·순서·연도 검증은 `day_range_to_utc` 가 이미 하므로 그것을 그대로 쓴다.
    """
    if not start_date and not end_date:
        return None
    if not start_date or not end_date:
        raise ValidationError("start_date and end_date must be provided together")
    day_range_to_utc(start_date, end_date)  # 형식 / 달력 / 역순 / 연도 범위 → ValidationError
    # ⚠️ 상한이 없으면 2000-01-01~2100-12-31 한 번이 캐시 없이 usage_logs 전체 집계를 6~9번 돈다.
    span_days = (_dt.date.fromisoformat(end_date) - _dt.date.fromisoformat(start_date)).days + 1
    if span_days > MAX_ANALYTICS_RANGE_DAYS:
        raise ValidationError(
            f"date range is {span_days} days; at most {MAX_ANALYTICS_RANGE_DAYS} days per request"
        )
    return start_date, end_date


class AnalyticsService:
    async def get_analytics(
        self,
        session: AsyncSession,
        *,
        period: str,
        group_by: str = "model",
        scope: str = "all",
        start_date: str | None = None,
        end_date: str | None = None,
        actor: CurrentUser,
    ) -> AnalyticsResponse:
        repo = AnalyticsRepository(session)

        # 일자 범위(옵션). 주어지면 **모든** 집계 — 요약/모델/팀/사용자/추이 — 가 이 범위로
        # 좁혀진다. 하나라도 월 필터로 남으면 같은 화면에서 카드 합계와 분해 합계가 어긋난다.
        date_range = _resolve_date_range(start_date, end_date)
        # ⚠️ 범위 모드에서도 period 형식은 검증한다. 월 필터를 타지 않는 경로라 검증이
        #    빠지면 period='garbage' 가 200 + 응답 라벨로 그대로 나간다(캐시 키에도 들어간다).
        period_to_utc_range(period)
        # ⚠️ 범위 모드의 period 는 **start_date 의 달**이어야 한다(admin-ui 는 그렇게 보낸다).
        #    period 는 응답 라벨·캐시 키·seed(월 상수) 판정에 쓰이므로, 범위와 다른 달이 오면 seed 를
        #    엉뚱한 달에서 더하거나 같은 범위가 period 별로 다른 캐시 키를 갖는다.
        if date_range and date_range[0][:7] != period:
            raise ValidationError(
                f"period {period} must be the month of start_date ({date_range[0][:7]})"
            )
        period_filter = cost_day_range_filter(*date_range) if date_range else cost_period_filter(period)

        # Determine scope filter
        #
        # ⚠️ scope 는 **닫힌 집합**('all' | 'team:<uuid>')으로만 해석한다 — 나머지는 전부 400.
        #    예전엔 `startswith("team:")` 와 `== "all"` 두 분기만 있었고 그 밖의 값('xyz',
        #    'ALL', ' all')은 어느 분기도 타지 않아 roi_scope=None(=GLOBAL)로 떨어졌다. 아래
        #    격리 불변식은 비-GLOBAL 일 때만 검사하므로 그대로 통과 → TEAM_LEADER 가 **전사**
        #    비용·사용자 이메일·팀을 받았고, export 는 그것을 CSV 로 내려줬다(실 PG 재현:
        #    다른 팀 사용자 $500 포함 $501). 'team:<uuid 아님>' 은 uuid.UUID 의 ValueError 가
        #    잡히지 않아 500 이었다. 라우트도 같은 집합을 pattern 으로 강제하지만(422), 서비스는
        #    라우트 밖에서도 불릴 수 있으므로 여기서도 스스로 닫는다.
        roi_scope: ROIScope | None = None
        scope_id: uuid.UUID | None = None

        if scope.startswith("team:"):
            try:
                team_id = uuid.UUID(scope[len("team:"):])
            except ValueError:
                raise ValidationError(f"scope must be 'all' or 'team:<uuid>' (got {scope!r})") from None
            # TEAM_LEADER can only see own team
            if actor.role == UserRole.TEAM_LEADER and actor.team_id != team_id:
                raise ForbiddenError("Team leaders can only view analytics for their own team")
            roi_scope = ROIScope.TEAM
            scope_id = team_id
        elif scope != "all":
            raise ValidationError(f"scope must be 'all' or 'team:<uuid>' (got {scope!r})")
        else:
            # TEAM_LEADER restricted to own team
            if actor.role == UserRole.TEAM_LEADER:
                # ⚠️ 팀이 없는 TEAM_LEADER 를 통과시키면 안 된다. 예전엔 scope_id 가
                #    None 이 되고, 아래 모든 WHERE 가 `if scope_id`/`is not None` 가드
                #    뒤에 있어서 **전부 사라졌다** → 전사 분석(비용·사용자·모델·추이)과
                #    /admin/analytics/export CSV 가 그대로 나갔다. 도달 경로: JWT 에
                #    team_id 클레임이 없으면(core/auth.py:157) 또는 auth.users.team_id
                #    가 NULL 이면(nullable) team_id 는 None 이다. dev 토큰은 role 을
                #    본문에서 읽고 team_id 를 항상 None 으로 만들기 때문에
                #    `dev.{"role":"TEAM_LEADER"}` 하나로 재현된다.
                if actor.team_id is None:
                    raise ForbiddenError(
                        "Team leader has no team assigned — cannot scope analytics. "
                        "Ask an administrator to assign a team."
                    )
                roi_scope = ROIScope.TEAM
                scope_id = actor.team_id
        # ADMIN: no restriction

        # 불변식(방어 심층): TEAM_LEADER 는 어떤 경로로 와도 TEAM 범위로 끝나야 한다. 위 분기에
        # 새 값이 추가되면서 이 성질이 다시 깨지면, 데이터를 읽기 전에 여기서 닫는다.
        if actor.role == UserRole.TEAM_LEADER and roi_scope != ROIScope.TEAM:
            raise ForbiddenError("Team leaders can only view analytics for their own team")

        # 불변식: 비-GLOBAL scope 라면 scope_id 가 반드시 있다. 아래의 by_user/trends 는
        # `scope_id is not None` 로 격리를 걸기 때문에, 이 둘이 어긋나면 그 두 질의만
        # 조용히 전사로 넓어진다(repo 쪽은 이제 터진다). 한곳에서 못 박는다.
        # assert 를 쓰지 않는다 — python -O 로 사라지는 검사에 데이터 격리를 맡길 수 없다.
        if roi_scope not in (None, ROIScope.GLOBAL) and scope_id is None:
            raise ForbiddenError(
                f"Analytics scope isolation could not be applied (scope={roi_scope}) — "
                "refusing to return organization-wide data."
            )

        # Real-time aggregation from usage_logs (not pre-aggregated roi_aggregations)
        query_scope = roi_scope or ROIScope.GLOBAL

        cost_by_model = await repo.sum_usage_by_model(
            period, query_scope, scope_id, date_range=date_range
        )
        # ⚠️ 같은 scope 필터(_apply_scope_filter)를 재사용하는 repo 메서드로 뽑는다 —
        #    WHERE 를 손으로 다시 쓰면 TEAM_LEADER 격리가 갈라진다.
        requests_by_model = await repo.count_requests_by_model(
            period, query_scope, scope_id, date_range=date_range
        )
        total_cost = sum(cost_by_model.values(), Decimal("0"))
        active_users_count = await repo.count_active_users(
            period, query_scope, scope_id, date_range=date_range
        )
        total_requests_count = await repo.total_requests(
            period, query_scope, scope_id, date_range=date_range
        )
        total_tokens_count = await repo.total_tokens(
            period, query_scope, scope_id, date_range=date_range
        )

        avg_cost = total_cost / active_users_count if active_users_count > 0 else Decimal("0")

        cost_summary = CostSummary(
            total_requests=total_requests_count,
            total_tokens=total_tokens_count,
            total_cost_usd=total_cost,
            active_users=active_users_count,
            avg_cost_per_user_usd=avg_cost,
        )

        # requests 를 채운다 — 예전엔 기본값 0 이 그대로 나가서, Analytics 화면에서
        # 내려받는 JSON export 가 모든 모델에 대해 "요청 0건" 을 보고했다.
        by_model = [
            ModelBreakdown(
                model=model,
                cost_usd=cost,
                requests=requests_by_model.get(model, 0),
            )
            for model, cost in cost_by_model.items()
        ]

        # Team breakdown — aggregate per team from usage_logs
        by_team: list[TeamBreakdown] = []
        if not roi_scope or roi_scope == ROIScope.GLOBAL:
            # (예전엔 여기서 sum_usage_by_model 을 team_costs 로 받아놓고 한 번도 읽지
            #  않았다 — group_by=team 요청마다 전체 테이블 집계를 낭비했으므로 제거.)
            from sqlalchemy import distinct, func, select
            from app.models.auth import Team
            from app.models.usage import UsageLog
            # ⚠️ team 라벨에 UUID 를 넣지 말 것 — 차트 x축에 그대로 노출된다.
            #    INNER JOIN 이 안전한 근거: usage_logs.team_id 는 NOT NULL + auth.teams.id
            #    FK (app/models/usage.py) 이므로 조인으로 사라지는 행이 없다(합계 불변).
            stmt = select(
                UsageLog.team_id,
                Team.name.label("team_name"),
                func.sum(UsageLog.cost_usd).label("cost"),
                func.count(distinct(UsageLog.user_id)).label("users"),
            ).join(
                Team, Team.id == UsageLog.team_id
            ).where(
                period_filter,  # §59 SUCCESS + KST (team 귀속은 usage_logs.team_id 직접)
            ).group_by(UsageLog.team_id, Team.name)
            result = await session.execute(stmt)
            for row in result:
                if row.team_id:
                    by_team.append(TeamBreakdown(
                        team=row.team_name or str(row.team_id),
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

            user_where = [period_filter]
            if scope_id is not None:  # TEAM_LEADER/team scope 격리
                user_where.append(UsageLog.team_id == scope_id)
            ustmt = (
                select(
                    User.id.label("user_id"),
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
            # ── 마이그레이션 주입분(seed) 반영 ──
            #
            # `/usage/by-user` 와 **같은 공식**을 쓴다(그 엔드포인트의 주석에 왜 이 형태인지
            # 적어 두었다). 여기는 기간이 월 단위(cost_period_filter)라 일자 보정이 필요
            # 없어 더 단순하다: seed = max(budget_usages 월 누적 − 월 실사용(전 status), 0).
            #
            # 이 차트에 넣어야 하는 이유: 이건 사용자가 실제로 보는 화면이다. seed 를 빼면
            # 이관된 사용자는 차트에서 싸 보이는데 예산 페이지는 96% 소진을 말한다 —
            # 같은 화면 안에서 서로 다른 사실을 주장하는 상태다. 순위도 seed 를 포함해야
            # "비용 상위 50명" 이라는 축이 맞는다.
            #
            # ⚠️ 팀 격리를 여기서도 지켜야 한다. budget_usages 에는 team_id 가 없으므로
            #    auth.users 로 조인해 걸러야 한다 — 안 하면 TEAM_LEADER 가 다른 팀 사용자의
            #    이관 금액을 받아 가는 권한 누출이 된다(위 user_where 격리와 같은 이유).
            #
            # ⚠️ 일자 범위 질의에는 **월 초 시작 범위(period 1일 ~ 같은 달)일 때만** seed 를
            #    더한다. seed 에는 일자 축이 없다(budget_usages 는 월 누적 한 행) — 그래서
            #    `/usage/by-user` 와 같은 규약("period 1일 ~ date" 누적에만 월 상수로 더한다)을
            #    따른다. 9/10~9/15 같은 중간 범위에 월 seed 를 더하면 이관 이전(= 월 초)의
            #    금액이 그 5일에 귀속되는 거짓 숫자가 된다. 그 범위는 usage_logs 사실만 보인다.
            from app.models.budget import BudgetScope, BudgetUsage

            seed_applies = date_range is None or (
                date_range[0] == f"{period}-01" and date_range[1][:7] == period
            )
            seeded: dict[uuid.UUID, tuple[str | None, str | None, Decimal]] = {}
            if seed_applies:
                m_start, m_end = period_to_utc_range(period)

                # 월 실사용(전 status) — 잔차의 뺄 값.
                actual_where = [
                    UsageLog.requested_at >= m_start,
                    UsageLog.requested_at < m_end,
                ]
                if scope_id is not None:
                    actual_where.append(UsageLog.team_id == scope_id)
                actual_stmt = (
                    select(
                        UsageLog.user_id.label("user_id"),
                        func.coalesce(func.sum(UsageLog.cost_usd), 0).label("actual"),
                    )
                    .where(*actual_where)
                    .group_by(UsageLog.user_id)
                )
                month_actual = {
                    r.user_id: Decimal(str(r.actual))
                    for r in (await session.execute(actual_stmt)).all()
                }

                recorded_stmt = (
                    select(
                        User.id.label("user_id"),
                        User.display_name.label("name"),
                        User.email.label("email"),
                        func.coalesce(func.sum(BudgetUsage.used_usd), 0).label("used"),
                    )
                    .select_from(BudgetUsage)
                    .join(User, User.id == BudgetUsage.scope_id)
                    .where(
                        BudgetUsage.scope == BudgetScope.USER,
                        BudgetUsage.period == period,
                    )
                    .group_by(User.id, User.display_name, User.email)
                )
                if scope_id is not None:
                    recorded_stmt = recorded_stmt.where(User.team_id == scope_id)

                for r in (await session.execute(recorded_stmt)).all():
                    residual = Decimal(str(r.used)) - month_actual.get(r.user_id, Decimal("0"))
                    if residual > 0:
                        seeded[r.user_id] = (r.name, r.email, residual)

            rows = (await session.execute(ustmt)).all()
            for row in rows:
                extra = seeded.pop(row.user_id, None)
                by_user.append(UserBreakdown(
                    user=row.name,
                    email=row.email,
                    cost_usd=(row.cost or Decimal("0")) + (extra[2] if extra else Decimal("0")),
                    requests=row.requests or 0,
                ))
            # usage_logs 에 한 건도 없이 seed 만 있는 사용자(이관 직후) — 목록의 기준
            # 테이블이 usage_logs 라 그냥 두면 차트에서 사라진다.
            for name, email, amount in seeded.values():
                by_user.append(UserBreakdown(
                    user=name, email=email, cost_usd=amount, requests=0
                ))

            # seed 를 더한 뒤 다시 상위 50명을 고른다 — SQL 의 ORDER BY/LIMIT 은 실사용
            # 기준이었다. 이 재정렬이 없으면 seed 가 큰 사용자가 51위에 밀려 안 보인다.
            by_user.sort(key=lambda b: b.cost_usd, reverse=True)
            del by_user[50:]

        # 비용 추이 — KST 일 버킷(§59). 예전엔 trends 를 아예 대입하지 않아서 기본값 []
        # 이 나갔고, 대시보드/Analytics 의 두 추이 차트가 옆 KPI 는 실제 금액을 보여주는
        # 동안 영구히 "데이터 없음" 을 렌더했다.
        # ⚠️ scope_id 격리는 by_user 와 **동일 규칙**으로. 이걸 빼면 TEAM_LEADER 가
        #    전사 일별 비용을 받아 가는 권한 누출이 된다.
        from sqlalchemy import func, select

        from app.models.usage import UsageLog

        _kst_day = func.date(func.timezone("Asia/Seoul", UsageLog.requested_at))
        # 범위가 주어지면 그 범위만 — 월로 남겨 두면 5일 범위 화면의 추이 차트에 end_date
        # 이후 날짜가 계속 그려진다.
        trend_where = [period_filter]
        if scope_id is not None:
            trend_where.append(UsageLog.team_id == scope_id)
        trend_stmt = (
            select(
                _kst_day.label("day"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
                func.count().label("requests"),
            )
            .where(*trend_where)
            .group_by(_kst_day)
            .order_by(_kst_day)
        )
        trends = [
            TrendItem(
                date=str(r.day),  # 'YYYY-MM-DD' — CostTrendCard 가 slice(5) 로 잘라 쓴다
                cost_usd=r.cost_usd or Decimal("0"),
                requests=r.requests or 0,
            )
            for r in (await session.execute(trend_stmt)).all()
        ]

        return AnalyticsResponse(
            period=period,
            start_date=date_range[0] if date_range else None,
            end_date=date_range[1] if date_range else None,
            cost_summary=cost_summary,
            by_model=by_model,
            by_team=by_team,
            by_user=by_user,
            trends=trends,
        )

    async def get_usage_by_user_model(
        self,
        session: AsyncSession,
        *,
        period: str,
        date: str,
    ) -> UsageByUserModelResponse:
        """User × Model 누적 (period 1일 ~ date, KST, SUCCESS only)."""
        from sqlalchemy import func, select

        from app.core.usage_filters import kst_day_range_filter
        from app.models.auth import Department, Team, User
        from app.models.usage import UsageLog, UsageStatus

        _validate_period_date(period, date)

        period_start = f"{period}-01"

        stmt = (
            select(
                UsageLog.user_id.label("user_id"),
                User.display_name.label("user_name"),
                User.email.label("user_email"),
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
                # ⚠️ `date(timezone('Asia/Seoul', requested_at)) >= date(:start)` 형태는
                #    좌변이 컬럼에 함수를 씌운 표현식이라 requested_at 인덱스를 못 타고
                #    usage_logs 를 전부 훑는다(월 필터를 cost_period_filter 로 고친 것과
                #    같은 이유). 경계를 파라미터 쪽에서 UTC 반개구간으로 환산하면 컬럼이
                #    그대로 남아 인덱스를 탄다. 집합은 동일하므로 숫자는 움직이지 않는다:
                #    KST 일자 ∈ [start, date] ⟺ requested_at ∈ [KST start, KST date+1).
                kst_day_range_filter(period_start, date),
            )
            .group_by(
                UsageLog.user_id,
                User.display_name,
                User.email,
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
                user_email=r.user_email,
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

        from app.core.usage_filters import kst_day_range_filter
        from app.models.auth import Department, Team, User
        from app.models.usage import UsageLog, UsageStatus

        _validate_period_date(period, date)

        period_start = f"{period}-01"

        stmt = (
            select(
                UsageLog.user_id.label("user_id"),
                User.display_name.label("user_name"),
                User.email.label("user_email"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
                func.count().label("calls"),
                func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("cache_read_tokens"),
                func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label(
                    "cache_creation_tokens"
                ),
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
                # ⚠️ `date(timezone('Asia/Seoul', requested_at)) >= date(:start)` 형태는
                #    좌변이 컬럼에 함수를 씌운 표현식이라 requested_at 인덱스를 못 타고
                #    usage_logs 를 전부 훑는다(월 필터를 cost_period_filter 로 고친 것과
                #    같은 이유). 경계를 파라미터 쪽에서 UTC 반개구간으로 환산하면 컬럼이
                #    그대로 남아 인덱스를 탄다. 집합은 동일하므로 숫자는 움직이지 않는다:
                #    KST 일자 ∈ [start, date] ⟺ requested_at ∈ [KST start, KST date+1).
                kst_day_range_filter(period_start, date),
            )
            .group_by(
                UsageLog.user_id,
                User.display_name,
                User.email,
                Team.id,
                Team.name,
                Department.id,
                Department.name,
            )
            .order_by(func.sum(UsageLog.cost_usd).desc())
        )

        rows = (await session.execute(stmt)).all()

        # ── 마이그레이션 주입분(seed) 반영 ──
        #
        # 문제: `POST /admin/budgets/seed-spent` 는 이관 이전 사용액을
        # `budget.budget_usages` 에 절대값으로 넣는데, 분석 경로는 `usage_logs` 만 읽었다.
        # 그래서 예산 화면은 $963 을 보여주고 분석 화면은 $13 을 보여줬다 — 같은 사용자,
        # 같은 기간, 서로 다른 숫자. 운영자가 어느 쪽을 믿어야 할지 알 수 없다.
        #
        # 왜 `max(실사용, budget_usages)` 가 아닌가(그 형태를 먼저 써 봤다면 안 되는 이유):
        #   * `budget_usages` 에는 **일자 축이 없다.** 월 누적 한 행뿐이다.
        #   * 그리고 모든 status 를 누적한다 — 분석은 SUCCESS 만 센다.
        #   ⇒ `date` 를 월 중간으로 주면 월 총액이 거의 항상 이겨서, 바깥 cost_usd 는
        #     date 를 **무시하고** 월 총액을 돌려주는 반면 calls/토큰은 date 범위였다.
        #     한 응답 안에서 두 필드가 다른 기간을 말하는 상태가 된다.
        #
        # 그래서 seed 를 **월 단위 상수**로 역산한다:
        #     seed := max(budget_usages 월 누적 − 같은 달 실사용(전 status), 0)
        # 이 값은 usage_logs 로 설명되지 않는 잔차 = 이관 금액이다. 여기에 일자 범위
        # 실사용을 더하면 date 는 지켜지고 seed 금액은 보존된다.
        #
        # ⚠️ 전 status 로 빼는 것이 load-bearing 이다. SUCCESS 만으로 빼면 실패 요청의
        #    비용이 잔차에 남아 seed 를 과대계상한다(budget_usages 는 전 status 누적).
        from sqlalchemy import text as sa_text

        from app.core.usage_filters import period_to_utc_range

        month_start_utc, month_end_utc = period_to_utc_range(period)

        seed_rows = (
            await session.execute(
                sa_text(
                    """
                    WITH month_actual AS (
                        SELECT user_id, COALESCE(SUM(cost_usd), 0) AS actual
                        FROM usage.usage_logs
                        WHERE requested_at >= :utc_start AND requested_at < :utc_end
                        GROUP BY user_id
                    ),
                    recorded AS (
                        SELECT scope_id AS user_id, COALESCE(SUM(used_usd), 0) AS used
                        FROM budget.budget_usages
                        WHERE scope = 'USER' AND period = :period
                        GROUP BY scope_id
                    )
                    SELECT r.user_id,
                           GREATEST(r.used - COALESCE(m.actual, 0), 0) AS seeded
                    FROM recorded r
                    LEFT JOIN month_actual m ON m.user_id = r.user_id
                    WHERE GREATEST(r.used - COALESCE(m.actual, 0), 0) > 0
                    """
                ),
                # 월 전체(1일~말일)를 UTC 반개구간으로. 일자 범위 필터와 같은 규약이라
                # 인덱스를 그대로 탄다(kst_day_range_filter 주석 참조).
                {
                    "period": period,
                    "utc_start": month_start_utc,
                    "utc_end": month_end_utc,
                },
            )
        ).all()
        seeded_by_user = {str(r.user_id): Decimal(str(r.seeded)) for r in seed_rows}

        items = [
            UsageByUserItem(
                date=date,
                user_id=str(r.user_id),
                user_name=r.user_name,
                user_email=r.user_email,
                cost_usd=r.cost_usd + seeded_by_user.get(str(r.user_id), Decimal("0")),
                seeded_usd=seeded_by_user.get(str(r.user_id), Decimal("0")),
                calls=r.calls,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cache_read_tokens=r.cache_read_tokens,
                cache_write_tokens=r.cache_creation_tokens,
                department_id=str(r.department_id) if r.department_id else None,
                department_name=r.department_name,
                team_id=str(r.team_id) if r.team_id else None,
                team_name=r.team_name,
            )
            for r in rows
        ]

        # ⚠️ usage_logs 에 한 건도 없는데 seed 만 있는 사용자가 존재한다(이관 직후, 아직
        #    게이트웨이를 쓰지 않은 사람). 위 목록은 usage_logs 기준이라 그 사람이 통째로
        #    빠진다 — 예산은 소진됐는데 분석에는 없는, 설명 불가능한 상태다.
        seen = {it.user_id for it in items}
        missing = [uid for uid in seeded_by_user if uid not in seen]
        if missing:
            from app.models.auth import Department as _Dept
            from app.models.auth import Team as _Team
            from app.models.auth import User as _User

            meta_rows = (
                await session.execute(
                    select(
                        _User.id,
                        _User.display_name,
                        _User.email,
                        _Team.id.label("team_id"),
                        _Team.name.label("team_name"),
                        _Dept.id.label("dept_id"),
                        _Dept.name.label("dept_name"),
                    )
                    .select_from(_User)
                    .outerjoin(_Team, _Team.id == _User.team_id)
                    .outerjoin(_Dept, _Dept.id == _Team.dept_id)
                    .where(_User.id.in_([uuid.UUID(u) for u in missing]))
                )
            ).all()
            for m in meta_rows:
                seeded = seeded_by_user[str(m.id)]
                items.append(
                    UsageByUserItem(
                        date=date,
                        user_id=str(m.id),
                        user_name=m.display_name,
                        user_email=m.email,
                        cost_usd=seeded,
                        seeded_usd=seeded,
                        calls=0,
                        department_id=str(m.dept_id) if m.dept_id else None,
                        department_name=m.dept_name,
                        team_id=str(m.team_id) if m.team_id else None,
                        team_name=m.team_name,
                    )
                )

        # seed 를 더한 뒤 다시 정렬한다 — SQL 의 ORDER BY 는 실사용 기준이었다.
        items.sort(key=lambda it: it.cost_usd, reverse=True)
        return UsageByUserResponse(period=period, date=date, items=items)

