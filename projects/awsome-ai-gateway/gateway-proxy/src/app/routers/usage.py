# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.models.usage import DailyAggregate
from app.schemas.domain import Role
from app.schemas.responses import DailyBreakdown, UsageBudgetInfo, UsageByModel, UsageMeResponse

logger = structlog.get_logger(__name__)

router = APIRouter()


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

    if period is None:
        period = datetime.now(tz=timezone.utc).strftime("%Y-%m")

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
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
    from datetime import date as date_cls

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

    # Budget 정보
    budget_info = UsageBudgetInfo(
        max_usd=Decimal("0"),
        used_usd=Decimal("0"),
        remaining_usd=Decimal("0"),
        pct=0.0,
        policy="hard_block",
    )
    if redis is not None:
        try:
            budget_config_raw = await redis.get(f"budget:config:user:{{{user_id}}}")
            budget_usage_raw = await redis.get(f"budget:user:{{{user_id}}}:{period}")
            if budget_config_raw:
                import json

                config = json.loads(budget_config_raw)
                limit = Decimal(str(config.get("limit_usd", 0)))
                used = Decimal(budget_usage_raw.decode() if budget_usage_raw else "0")
                remaining = limit - used
                pct = float((used / limit * 100) if limit > 0 else 0)
                budget_info = UsageBudgetInfo(
                    max_usd=limit,
                    used_usd=used,
                    remaining_usd=remaining,
                    pct=round(pct, 2),
                    policy=config.get("policy", "hard_block"),
                )
        except Exception:
            logger.warning("budget_info_fetch_failed", user_id=user_id)

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
    period = datetime.now(tz=timezone.utc).strftime("%Y-%m")

    # 팀 사용량 집계 (간략 버전)
    total_tokens = 0
    total_cost = Decimal("0")

    if session_factory is not None:
        try:
            async with session_factory() as db_session:
                result = await db_session.execute(
                    select(DailyAggregate)
                    .where(DailyAggregate.team_id == team_id)
                    .where(DailyAggregate.date.like(f"{period}%"))
                )
                for agg in result.scalars().all():
                    total_tokens += agg.total_tokens
                    total_cost += agg.total_cost_usd
        except Exception:
            logger.warning("team_usage_fetch_failed", team_id=team_id)

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
