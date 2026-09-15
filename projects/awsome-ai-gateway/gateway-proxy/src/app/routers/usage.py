# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import date as date_cls
from decimal import Decimal

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.models.usage import DailyAggregate
from app.periods import current_kst_date, current_kst_period
from app.schemas.domain import Role
from app.schemas.responses import DailyBreakdown, UsageBudgetInfo, UsageByModel, UsageMeResponse

logger = structlog.get_logger(__name__)

router = APIRouter()


def _db_policy_label(policy) -> str:
    """DB ``budget_policy`` enum → 응답의 소문자 라벨.

    Redis 캐시에는 이미 소문자 문자열이 들어 있으므로(``budget_service`` 가 변환해서
    넣는다), DB 로 내려간 경로만 여기서 맞춰 준다. 두 경로가 다른 표기를 내보내면
    클라이언트가 정책에 따라 분기할 때 조용히 어긋난다.
    """
    raw = getattr(policy, "value", policy)
    return str(raw).lower()



@router.get("/v1/usage/me")
async def usage_me(
    request: Request,
    period: str = Query(default=None, description="YYYY-MM, default: current month"),
) -> JSONResponse:
    state = request.scope.get("state", {})
    auth_context = state.get("auth_context")
    if auth_context is None:
        return JSONResponse(status_code=401, content={"error": {"type": "unauthorized"}})

    redis = state.get("_redis")
    session_factory = state.get("_session_factory")

    # ⚠️ 둘 다 **KST** 경계다(app.periods 참조). 아래에서 이 값들이
    #   * period → budget:user:{uid}:{period} 조회 + daily_aggregates 월 하한
    #   * today  → usage:daily:user:{uid}:{today} 조회 + daily_aggregates 상한
    # 으로 쓰이는데, 그 두 소스가 모두 KST 로 버킷돼 있다. UTC 로 잡으면 KST 09:00
    # 이전에 하루치가 통째로 사라진다(current_kst_date docstring 에 상세).
    if period is None:
        period = current_kst_period()

    today = current_kst_date()
    user_id = auth_context.user_id

    # 당일 Redis 집계
    total_tokens_today = 0
    total_cost_today = Decimal("0")
    if redis is not None:
        try:
            # Redis Cluster hash tag on {user_id}: all daily keys for this user
            # co-locate to a single slot (see cost_recorder).
            daily_prefix = f"usage:daily:user:{{{user_id}}}:{today}"
            cost_val = await redis.get(f"{daily_prefix}:cost")
            tokens_val = await redis.get(f"{daily_prefix}:tokens")
            if cost_val:
                total_cost_today = Decimal(cost_val.decode())
            if tokens_val:
                total_tokens_today = int(tokens_val)
        except Exception:
            logger.warning("redis_usage_fetch_failed", user_id=user_id)

    # 이전 일자(어제 이전) DB daily_aggregates 조회. Granularity는 (date, user, model_alias)
    # 이므로 같은 date에 모델별 다수 row가 나옴 → date 기준 GROUP BY (in-Python)로
    # daily_breakdown은 1 row/date.
    # period는 YYYY-MM, today 는 YYYY-MM-DD.
    from collections import defaultdict

    total_tokens_db = 0
    total_cost_db = Decimal("0")
    daily_breakdown = []

    if session_factory is not None:
        try:
            try:
                period_year, period_month = (int(x) for x in period.split("-", 1))
                period_start = date_cls(period_year, period_month, 1)
            except ValueError:
                period_start = None

            if period_start is not None:
                today_date = date_cls.fromisoformat(today)
                async with session_factory() as db_session:
                    result = await db_session.execute(
                        select(DailyAggregate)
                        .where(DailyAggregate.user_id == user_id)
                        .where(DailyAggregate.date >= period_start)
                        .where(DailyAggregate.date < today_date)
                        .order_by(DailyAggregate.date)
                    )
                    aggregates = result.scalars().all()
                by_date: dict[str, dict] = defaultdict(
                    lambda: {"tokens": 0, "cost": Decimal("0")}
                )
                for agg in aggregates:
                    day = agg.date.isoformat()
                    by_date[day]["tokens"] += agg.total_tokens
                    by_date[day]["cost"] += agg.total_cost_usd
                    total_tokens_db += agg.total_tokens
                    total_cost_db += agg.total_cost_usd
                for day in sorted(by_date.keys()):
                    v = by_date[day]
                    daily_breakdown.append(
                        DailyBreakdown(
                            date=day,
                            total_tokens=v["tokens"],
                            total_cost_usd=v["cost"],
                        )
                    )
        except Exception:
            logger.warning("db_usage_fetch_failed", user_id=user_id)

    # ── Budget 정보 ─────────────────────────────────────────────────────────────
    #
    # ⚠️ ``budget:config:user:{uid}`` 는 **TTL 300초**다(budget_service 가 캐시 미스 시
    #    재생성한다). 즉 만료는 예외 상황이 아니라 **정상 동작**이다.
    #
    #    예전에는 그 키가 없으면 이 블록 전체를 건너뛰어 max/used/remaining 이 모두 0 인
    #    기본값이 그대로 응답에 실렸다. 그래서 5분 넘게 요청을 보내지 않은 사용자가 CLI
    #    statusline 을 열면 "$0 / $0" 을 봤다 — 한도도 없고 쓴 것도 없다는 뜻이다. 실제로는
    #    한도가 있고 이미 상당액을 썼는데도.
    #
    #    더 나쁜 것은 ``used`` 조차 config 가 있을 때만 읽었다는 점이다. 사용액 키
    #    ``budget:user:{uid}:{period}`` 에 진짜 값이 들어 있어도 함께 버려졌다.
    #
    #    이제 Redis 를 먼저 보고(실시간), 없거나 비면 DB 로 내려간다. DB 는 워커가
    #    ``budget_usages`` 에 커밋한 값이라 최대 몇 초 뒤처질 뿐 사라지지 않는다.
    limit: Decimal | None = None
    used: Decimal | None = None
    policy = "hard_block"

    if redis is not None:
        try:
            budget_config_raw = await redis.get(f"budget:config:user:{{{user_id}}}")
            budget_usage_raw = await redis.get(f"budget:user:{{{user_id}}}:{period}")
            if budget_config_raw:
                import json

                config = json.loads(budget_config_raw)
                limit = Decimal(str(config.get("limit_usd", 0)))
                policy = config.get("policy", "hard_block")
            if budget_usage_raw:
                used = Decimal(
                    budget_usage_raw.decode()
                    if isinstance(budget_usage_raw, bytes)
                    else str(budget_usage_raw)
                )
        except Exception:
            logger.warning("budget_info_fetch_failed", user_id=user_id)

    # Redis 에서 못 채운 것만 DB 로 메운다. 조회 자체가 실패해도 응답은 나가야 한다 —
    # 예산 표시가 없다고 사용량 조회를 실패시킬 이유는 없다.
    if session_factory is not None and (limit is None or used is None):
        try:
            from app.models.budget import BudgetConfig, BudgetScope, BudgetUsage

            async with session_factory() as db_session:
                if limit is None:
                    # client IS NULL = 사용자 전체 예산. 이 필터가 없으면 per-app 예산 행이
                    # 잡혀 앱 하나의 한도가 사용자 전체 한도로 보고된다.
                    cfg = (
                        await db_session.execute(
                            select(BudgetConfig)
                            .where(BudgetConfig.scope == BudgetScope.USER)
                            .where(BudgetConfig.scope_id == user_id)
                            .where(BudgetConfig.client.is_(None))
                            .where(BudgetConfig.is_active == True)  # noqa: E712
                            # ⚠️ 의도적으로 LIMIT 을 걸지 않는다. per-app 행이 있는
                            #    사용자에서 위 client 필터가 빠지면 두 행이 나와
                            #    MultipleResultsFound 로 **시끄럽게** 실패한다. LIMIT 1 을
                            #    걸면 물리적 순서에 따라 우연히 맞는 행이 나올 수 있고,
                            #    그러면 필터를 지워도 테스트가 통과한다(대조군으로 확인).
                            #    budget_service._hydrate_user_config_cache 와 같은 이유다.
                        )
                    ).scalar_one_or_none()
                    if cfg is not None:
                        limit = Decimal(str(cfg.max_budget_usd))
                        policy = _db_policy_label(cfg.policy)
                if used is None:
                    row = (
                        await db_session.execute(
                            select(BudgetUsage.used_usd)
                            .where(BudgetUsage.scope == BudgetScope.USER)
                            .where(BudgetUsage.scope_id == user_id)
                            .where(BudgetUsage.client.is_(None))
                            .where(BudgetUsage.period == period)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        used = Decimal(str(row))
        except Exception:
            logger.warning("budget_info_db_fallback_failed", user_id=user_id)

    limit_final = limit if limit is not None else Decimal("0")
    used_final = used if used is not None else Decimal("0")
    budget_info = UsageBudgetInfo(
        max_usd=limit_final,
        used_usd=used_final,
        remaining_usd=limit_final - used_final,
        pct=round(float(used_final / limit_final * 100) if limit_final > 0 else 0.0, 2),
        policy=policy,
    )

    total_tokens = total_tokens_db + total_tokens_today
    total_cost = total_cost_db + total_cost_today

    # Model breakdown from Redis (today's data)
    model_breakdown = []
    if redis is not None:
        try:
            daily_prefix = f"usage:daily:user:{{{user_id}}}:{today}"
            model_names = await redis.smembers(f"{daily_prefix}:models")
            for raw_name in model_names or []:
                name = raw_name.decode() if isinstance(raw_name, bytes) else raw_name
                mp = f"{daily_prefix}:model:{name}"
                pipe = redis.pipeline()
                pipe.get(f"{mp}:cost")
                pipe.get(f"{mp}:input")
                pipe.get(f"{mp}:output")
                pipe.get(f"{mp}:cache_write")
                pipe.get(f"{mp}:cache_read")
                pipe.get(f"{mp}:requests")
                vals = await pipe.execute()
                model_breakdown.append(
                    {
                        "model": name,
                        "cost_usd": vals[0].decode() if vals[0] else "0",
                        "input_tokens": int(vals[1] or 0),
                        "output_tokens": int(vals[2] or 0),
                        "cache_write_tokens": int(vals[3] or 0),
                        "cache_read_tokens": int(vals[4] or 0),
                        "requests": int(vals[5] or 0),
                    }
                )
        except Exception:
            logger.warning("model_breakdown_fetch_failed", user_id=user_id)

    response = UsageMeResponse(
        user_id=user_id,
        period=period,
        usage={
            "total_tokens": total_tokens,
            "total_cost_usd": str(total_cost),
        },
        budget=budget_info,
        daily_breakdown=daily_breakdown,
    )
    content = response.model_dump(mode="json")
    content["model_breakdown"] = model_breakdown
    return JSONResponse(content=content)


@router.get("/v1/usage/team/{team_id}")
async def usage_team(team_id: str, request: Request) -> JSONResponse:
    state = request.scope.get("state", {})
    auth_context = state.get("auth_context")
    if auth_context is None:
        return JSONResponse(status_code=401, content={"error": {"type": "unauthorized"}})

    # 권한 검사: TEAM_LEADER + 본인 팀, 또는 ADMIN
    is_admin = Role.ADMIN in auth_context.roles
    is_team_leader = Role.TEAM_LEADER in auth_context.roles and auth_context.team_id == team_id

    if not (is_admin or is_team_leader):
        return JSONResponse(
            status_code=403,
            content={"error": {"type": "permission_denied", "message": "Insufficient permissions"}},
        )

    session_factory = state.get("_session_factory")
    redis = state.get("_redis")
    # KST 월 — daily_aggregates.date 가 KST 로 버킷돼 있으므로 월 경계도 KST 여야
    # 한다. UTC 면 매월 1일 KST 00:00~09:00 동안 지난달 합계를 보여준다.
    period = current_kst_period()

    # 팀 사용량 집계 (간략 버전)
    total_tokens = 0
    total_cost = Decimal("0")

    if session_factory is not None:
        try:
            # ⚠️ `date` 는 DATE 컬럼이므로 문자열 LIKE 로 월을 걸러선 안 된다.
            # `DailyAggregate.date.like(f"{period}%")` 는 `date LIKE '2026-09%'` 를
            # 내보내고 PostgreSQL 이 거부한다:
            #   operator does not exist: date ~~ unknown   (실 PG16 확인)
            # 아래 except 가 이 예외를 삼켜 이 엔드포인트는 항상 0/0 을 돌려줬다.
            # 반열구간(>= 월초, < 다음달초)으로 비교해야 인덱스
            # (idx_daily_aggregates_team_date) 도 그대로 탄다.
            period_start = date_cls(*(int(x) for x in period.split("-", 1)), 1)
            next_month = (
                date_cls(period_start.year + 1, 1, 1)
                if period_start.month == 12
                else date_cls(period_start.year, period_start.month + 1, 1)
            )
            async with session_factory() as db_session:
                result = await db_session.execute(
                    select(DailyAggregate)
                    .where(DailyAggregate.team_id == team_id)
                    .where(DailyAggregate.date >= period_start)
                    .where(DailyAggregate.date < next_month)
                )
                for agg in result.scalars().all():
                    total_tokens += agg.total_tokens
                    total_cost += agg.total_cost_usd
        except Exception:
            # warning + traceback 없음이면 위 같은 타입 드리프트가 조용히 0 으로 보인다.
            logger.exception("team_usage_fetch_failed", team_id=team_id)

    return JSONResponse(
        content={
            "team_id": team_id,
            "period": period,
            "usage": {
                "total_tokens": total_tokens,
                "total_cost_usd": str(total_cost),
            },
        }
    )
