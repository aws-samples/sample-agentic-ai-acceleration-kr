# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import get_db_session
from app.models.usage import GitEvent, GitEventType, ProductivityEvent, ProductivityEventType

router = APIRouter(tags=["Productivity"])


# ── Schemas ──


class ProductivityEventRequest(BaseModel):
    user_id: str
    team_id: str | None = None
    event_type: ProductivityEventType
    session_id: str | None = None
    model_alias: str | None = None
    lines_generated: int = Field(default=0, ge=0)
    lines_accepted: int = Field(default=0, ge=0)
    language: str | None = None


class GitWebhookPayload(BaseModel):
    """Simplified GitHub webhook payload — supports push & pull_request events."""
    pass


# ── POST /internal/productivity ──


@router.post("/internal/productivity")
async def record_productivity_event(
    request: Request,
    body: ProductivityEventRequest,
    session: AsyncSession = Depends(get_db_session),
):
    event = ProductivityEvent(
        id=uuid.uuid4(),
        user_id=uuid.UUID(body.user_id),
        team_id=uuid.UUID(body.team_id) if body.team_id else None,
        event_type=body.event_type,
        session_id=body.session_id,
        model_alias=body.model_alias,
        lines_generated=body.lines_generated,
        lines_accepted=body.lines_accepted,
        language=body.language,
        created_at=datetime.now(timezone.utc),
    )
    session.add(event)
    await session.commit()
    return {"status": "ok", "event_id": str(event.id)}


# ── POST /webhooks/git ──


@router.post("/webhooks/git")
async def receive_git_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    raw = await request.json()
    github_event = request.headers.get("X-GitHub-Event", "")

    events: list[GitEvent] = []

    if github_event == "push":
        commits = raw.get("commits", [])
        if not commits:
            return {"status": "ok", "recorded": 0}

        repo = raw.get("repository", {}).get("full_name", "unknown")
        ref = raw.get("ref", "")

        author_emails: dict[str, int] = {}
        for c in commits:
            email = c.get("author", {}).get("email", "unknown@unknown")
            author_emails[email] = author_emails.get(email, 0) + 1

        from sqlalchemy import select as sa_select
        from app.models.auth import User

        for email, count in author_emails.items():
            user_stmt = sa_select(User).where(User.email == email).limit(1)
            user_result = await session.execute(user_stmt)
            user = user_result.scalar_one_or_none()

            events.append(GitEvent(
                id=uuid.uuid4(),
                user_id=user.id if user else None,
                user_email=email,
                event_type=GitEventType.COMMIT,
                repo=repo,
                ref=ref,
                commit_count=count,
                created_at=datetime.now(timezone.utc),
            ))

    elif github_event == "pull_request":
        action = raw.get("action", "")
        if action not in ("opened", "closed"):
            return {"status": "ok", "recorded": 0}

        pr = raw.get("pull_request", {})
        merged = pr.get("merged", False)
        email = pr.get("user", {}).get("email") or raw.get("sender", {}).get("email", "unknown@unknown")
        repo = raw.get("repository", {}).get("full_name", "unknown")

        if action == "opened":
            event_type = GitEventType.PR_OPENED
        elif action == "closed" and merged:
            event_type = GitEventType.PR_MERGED
        else:
            return {"status": "ok", "recorded": 0}

        from sqlalchemy import select as sa_select
        from app.models.auth import User

        user_stmt = sa_select(User).where(User.email == email).limit(1)
        user_result = await session.execute(user_stmt)
        user = user_result.scalar_one_or_none()

        events.append(GitEvent(
            id=uuid.uuid4(),
            user_id=user.id if user else None,
            user_email=email,
            event_type=event_type,
            repo=repo,
            ref=pr.get("head", {}).get("ref", ""),
            commit_count=pr.get("commits", 0),
            created_at=datetime.now(timezone.utc),
        ))
    else:
        return {"status": "ignored", "event": github_event}

    for ev in events:
        session.add(ev)
    await session.commit()

    return {"status": "ok", "recorded": len(events)}


# ── GET /admin/analytics/productivity ──


@router.get("/admin/analytics/productivity")
async def get_productivity_analytics(
    request: Request,
    period: str | None = None,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):

    from datetime import date
    now = date.today()
    if not period:
        period = f"{now.year}-{now.month:02d}"

    # Productivity: code generation stats
    prod_stmt = (
        select(
            func.count().label("total_events"),
            func.coalesce(func.sum(ProductivityEvent.lines_generated), 0).label("total_lines_generated"),
            func.coalesce(func.sum(ProductivityEvent.lines_accepted), 0).label("total_lines_accepted"),
            func.count().filter(ProductivityEvent.event_type == ProductivityEventType.CODE_ACCEPTED).label("accepted_count"),
            func.count().filter(ProductivityEvent.event_type == ProductivityEventType.CODE_GENERATED).label("generated_count"),
        )
        .where(func.to_char(ProductivityEvent.created_at, "YYYY-MM") == period)
    )
    prod_result = await session.execute(prod_stmt)
    prod_row = prod_result.one()

    total_gen = prod_row.generated_count or 0
    total_acc = prod_row.accepted_count or 0
    acceptance_rate = (total_acc / total_gen * 100) if total_gen > 0 else 0

    # Git: commits and PRs
    commit_stmt = (
        select(
            func.coalesce(func.sum(GitEvent.commit_count), 0).label("total_commits"),
        )
        .where(
            func.to_char(GitEvent.created_at, "YYYY-MM") == period,
            GitEvent.event_type == GitEventType.COMMIT,
        )
    )
    commit_result = await session.execute(commit_stmt)
    total_commits = commit_result.scalar_one() or 0

    pr_opened_stmt = (
        select(func.count())
        .where(
            func.to_char(GitEvent.created_at, "YYYY-MM") == period,
            GitEvent.event_type == GitEventType.PR_OPENED,
        )
    )
    pr_opened = (await session.execute(pr_opened_stmt)).scalar_one() or 0

    pr_merged_stmt = (
        select(func.count())
        .where(
            func.to_char(GitEvent.created_at, "YYYY-MM") == period,
            GitEvent.event_type == GitEventType.PR_MERGED,
        )
    )
    pr_merged = (await session.execute(pr_merged_stmt)).scalar_one() or 0

    # Active developers (union of productivity + git)
    dev_prod = (
        select(ProductivityEvent.user_id)
        .where(func.to_char(ProductivityEvent.created_at, "YYYY-MM") == period)
    )
    dev_git = (
        select(GitEvent.user_id)
        .where(
            func.to_char(GitEvent.created_at, "YYYY-MM") == period,
            GitEvent.user_id.isnot(None),
        )
    )
    union_stmt = select(func.count()).select_from(dev_prod.union(dev_git).subquery())
    active_devs = (await session.execute(union_stmt)).scalar_one() or 0

    # Cost data for ROI calculation — §59 비용 집계 표준(SUCCESS + KST).
    from app.models.usage import UsageLog
    from app.core.usage_filters import cost_period_filter
    cost_stmt = (
        select(func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(cost_period_filter(period))
    )
    total_cost = float((await session.execute(cost_stmt)).scalar_one() or 0)

    lines_gen = int(prod_row.total_lines_generated or 0)
    lines_acc = int(prod_row.total_lines_accepted or 0)
    cost_per_line = (total_cost / lines_gen) if lines_gen > 0 else 0
    cost_per_commit = (total_cost / total_commits) if total_commits > 0 else 0

    return {
        "period": period,
        "productivity": {
            "total_lines_generated": lines_gen,
            "total_lines_accepted": lines_acc,
            "code_acceptance_rate_pct": round(acceptance_rate, 1),
            "total_commits": int(total_commits),
            "pr_opened": int(pr_opened),
            "pr_merged": int(pr_merged),
            "active_developers": int(active_devs),
        },
        "roi": {
            "total_cost_usd": round(total_cost, 4),
            "cost_per_generated_line": round(cost_per_line, 6),
            "cost_per_commit": round(cost_per_commit, 4),
        },
    }
