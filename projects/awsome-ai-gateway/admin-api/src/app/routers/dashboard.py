# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.core.usage_filters import (
    client_coalesce_expr,
    client_filter,
    cost_period_filter,
    kst_month_expr,
)
from app.models.auth import Team, User
from app.models.model import ModelAlias
from app.models.usage import UsageLog, UsageStatus
from zoneinfo import ZoneInfo

router = APIRouter(prefix="/admin/dashboard", tags=["Dashboard"])

_KST = ZoneInfo("Asia/Seoul")


def _default_period() -> str:
    # KST 기준(§59) — 데이터 월 버킷이 KST 이므로 기본 기간도 KST 로 통일.
    # (한국 운영 자산 — 모든 캘린더 경계는 Asia/Seoul.)
    return datetime.now(_KST).strftime("%Y-%m")


@router.get("/summary")
async def dashboard_summary(
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    if not period:
        period = _default_period()

    # 선택적 앱(client) 필터 — 'all'/None 이면 전체.
    where_clauses = [cost_period_filter(period)]
    if (cf := client_filter(client)) is not None:
        where_clauses.append(cf)

    stmt = select(
        func.count().label("total_requests"),
        func.coalesce(
            func.sum(
                UsageLog.input_tokens
                + UsageLog.output_tokens
                + UsageLog.cache_creation_tokens
                + UsageLog.cache_read_tokens
            ),
            0,
        ).label("total_tokens"),
        func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost_usd"),
        func.count(distinct(UsageLog.user_id)).label("active_users"),
    ).where(*where_clauses)
    row = (await session.execute(stmt)).one()

    total_requests = row.total_requests or 0
    total_tokens = int(row.total_tokens or 0)
    total_cost = Decimal(row.total_cost_usd or 0)
    active_users = row.active_users or 0
    cost_per_user = (total_cost / active_users) if active_users > 0 else Decimal(0)

    return {
        "period": period,
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "total_cost_usd": round(float(total_cost), 4),
        "active_users": active_users,
        "cost_per_user_usd": round(float(cost_per_user), 4),
    }


@router.get("/model-share")
async def model_share(
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    team_id: str = Query(default="all", description="UUID 또는 'all'"),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    if not period:
        period = _default_period()

    where_clauses = [
        # §59 비용 집계 표준: SUCCESS 만 + KST 월 경계.
        cost_period_filter(period),
    ]
    if (cf := client_filter(client)) is not None:
        where_clauses.append(cf)

    team_filter: str = "all"
    if team_id and team_id != "all":
        try:
            team_uuid = uuid.UUID(team_id)
        except ValueError:
            return {
                "period": period,
                "team_id": team_id,
                "total_cost_usd": 0.0,
                "models": [],
                "error": "invalid_team_id",
            }
        where_clauses.append(UsageLog.team_id == team_uuid)
        team_filter = str(team_uuid)

    # display_name 을 위해 model_aliases 와 LEFT OUTER JOIN (없으면 NULL → UI 가 alias fallback).
    stmt = (
        select(
            UsageLog.model_alias,
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
            func.max(ModelAlias.display_name).label("display_name"),
        )
        .select_from(UsageLog)
        .outerjoin(ModelAlias, ModelAlias.alias == UsageLog.model_alias)
        .where(*where_clauses)
        .group_by(UsageLog.model_alias)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    rows = (await session.execute(stmt)).all()

    total = sum(float(r.cost_usd or 0) for r in rows)
    models = []
    for r in rows:
        cost = float(r.cost_usd or 0)
        share = (cost / total * 100) if total > 0 else 0.0
        models.append({
            "model_alias": r.model_alias,
            "display_name": r.display_name,
            "cost_usd": round(cost, 4),
            "share_pct": round(share, 2),
        })

    return {
        "period": period,
        "team_id": team_filter,
        "total_cost_usd": round(total, 4),
        "models": models,
    }


@router.get("/client-share")
async def client_share(
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """앱별(client) 비용 점유율 — claude-code / cowork / codex / other(legacy NULL 포함).

    client_coalesce_expr() 로 NULL(레거시 미식별) 행을 'other' 로 접어 GROUP BY.
    §59 비용 집계 표준(SUCCESS + KST 월 경계) 동일 적용. admin-ui ClientShareResponse 형태.
    """
    if not period:
        period = _default_period()

    client_col = client_coalesce_expr().label("client")
    stmt = (
        select(
            client_col,
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
            func.count().label("call_count"),
            # Web search calls per client (attribution metric; not a cost/token). Lets the
            # dashboard show which apps use AgentCore WebSearch and how much.
            func.coalesce(func.sum(UsageLog.web_search_count), 0).label("web_search_count"),
        )
        .where(cost_period_filter(period))
        .group_by(client_col)
        .order_by(func.sum(UsageLog.cost_usd).desc())
    )
    rows = (await session.execute(stmt)).all()

    total = sum(float(r.cost_usd or 0) for r in rows)
    clients = []
    for r in rows:
        cost = float(r.cost_usd or 0)
        share = (cost / total * 100) if total > 0 else 0.0
        clients.append({
            "client": r.client,
            "cost_usd": round(cost, 4),
            "share_pct": round(share, 2),
            "call_count": int(r.call_count or 0),
            "web_search_count": int(r.web_search_count or 0),
        })

    return {
        "period": period,
        "total_cost_usd": round(total, 4),
        "clients": clients,
    }


@router.get("/top-users")
async def top_users(
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    limit: int = Query(default=5, ge=1, le=50),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """실제 비용 기준 상위 사용자(§60.8) — usage_logs 를 SUCCESS+KST 로 집계해 cost 내림차순.

    ⚠️ 기존 대시보드 'Top 사용자 by 비용' 위젯은 budgets/summary(예산설정된 사용자만)를
    써서 예산 없는 헤비유저를 누락했다(라벨='비용'인데 실제론 예산설정자 중 사용액).
    이 엔드포인트는 챗(text2SQL)과 동일하게 usage_logs 전체에서 진짜 top spender 를 낸다.
    PII 금지: sso_subject 미노출(display_name·email 만).
    """
    if not period:
        period = _default_period()

    stmt = (
        select(
            User.display_name.label("name"),
            User.email.label("email"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
            func.count().label("call_count"),
        )
        .join(User, User.id == UsageLog.user_id)
        .where(
            cost_period_filter(period),  # §59 SUCCESS + KST (대시보드 단일 진실원)
            *([cf] if (cf := client_filter(client)) is not None else []),
        )
        .group_by(User.id, User.display_name, User.email)
        .order_by(func.sum(UsageLog.cost_usd).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    return {
        "period": period,
        "users": [
            {
                "name": r.name,
                "email": r.email,
                "cost_usd": round(float(r.cost_usd or 0), 4),
                "call_count": int(r.call_count or 0),
            }
            for r in rows
        ],
    }


@router.get("/top-teams")
async def top_teams(
    period: str = Query(default=None, description="YYYY-MM (KST). 미지정 시 현재 월"),
    limit: int = Query(default=5, ge=1, le=50),
    client: str = Query(default=None, description="claude-code|cowork|codex|other|all"),
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """실제 비용 기준 상위 팀(§60.9) — usage_logs 를 SUCCESS+KST 로 집계해 cost 내림차순.

    기존 대시보드 'Top 팀 by 비용'은 budgets/summary(예산설정 팀만)를 써서 예산 없는
    팀을 누락했다(§60.8 의 top-users 와 동형 버그 — 팀은 미수정이었음). top-users 와
    동일하게 usage_logs 전체에서 집계한다. **팀 귀속은 usage_logs.team_id 직접**
    (users.team_id 경유 금지 — 팀 이동 사용자의 과거 비용 오귀속 방지).
    """
    if not period:
        period = _default_period()

    stmt = (
        select(
            Team.name.label("name"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("cost_usd"),
            func.count().label("call_count"),
        )
        .join(Team, Team.id == UsageLog.team_id)
        .where(
            cost_period_filter(period),  # §59 SUCCESS + KST (대시보드 단일 진실원)
            *([cf] if (cf := client_filter(client)) is not None else []),
        )
        .group_by(Team.id, Team.name)
        .order_by(func.sum(UsageLog.cost_usd).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    return {
        "period": period,
        "teams": [
            {
                "name": r.name,
                "cost_usd": round(float(r.cost_usd or 0), 4),
                "call_count": int(r.call_count or 0),
            }
            for r in rows
        ],
    }


@router.get("/periods")
async def dashboard_periods(
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """사용량 데이터가 실제로 존재하는 월(YYYY-MM) 목록 — 최신순.

    admin-ui 의 기간 선택기가 이걸로 옵션을 채우고, 기본값을
    "데이터 있는 가장 최근 월"(periods[0])로 잡아 현재 달력월이 비어도
    빈 화면을 피한다. status 필터 안 함 — 에러만 있는 월도 노출.
    """
    # 월 binning 을 명시적 KST 로(§59) — requested_at 은 timestamptz 라 to_char 가
    # 세션 타임존을 타므로, timezone('Asia/Seoul', ...) 로 고정해 /summary·budget·
    # chat 과 동일 기준(KST) 보장. status 필터 안 함 — 에러만 있는 월도 옵션에 노출.
    period_expr = kst_month_expr()
    stmt = select(distinct(period_expr).label("period")).order_by(period_expr.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return {"periods": [p for p in rows if p]}
