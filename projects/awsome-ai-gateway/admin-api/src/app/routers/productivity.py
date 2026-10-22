# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

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
    #: 재전송 중복 제거용. 생략하면 중복 제거를 하지 않는다(기존 호출자 무변경).
    idempotency_key: str | None = Field(default=None, max_length=256)


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
    # ── 재전송 중복 제거 ──
    #
    # 웹훅은 재전송된다(수신측 타임아웃·5xx·네트워크 단절). 중복 제거가 없으면 같은
    # 이벤트가 두 번 적재되어 ROI 지표(생성/수용 줄 수)가 그만큼 부풀려진다 — 조용히
    # 틀린 숫자가 되고, 나중에 원인을 역추적할 방법이 없다.
    #
    # ⚠️ SELECT 로 확인하고 INSERT 하는 것만으로는 **닫히지 않는다.** 동시 재전송 두
    #    건이 둘 다 "없다" 를 보고 둘 다 INSERT 하면, 부분 유니크 인덱스
    #    (idx_productivity_events_idempotency, migration 0034) 가 한쪽을 거부하고
    #    그 요청은 500 이 된다. 그래서 두 층으로 처리한다:
    #      1) 선-조회 — 흔한 경우(이미 적재된 재전송)에서 쓸데없는 INSERT 를 피한다.
    #      2) 세이브포인트 + IntegrityError — 경합으로 진 쪽도 "이미 처리됨" 으로
    #         **200** 을 돌려준다. 409 가 아니라 200 인 이유: 호출자의 의도(이 이벤트를
    #         기록해 달라)는 이미 충족됐다. 409 를 주면 웹훅 발신자는 실패로 보고 또
    #         재전송한다.
    if body.idempotency_key:
        from sqlalchemy import select as sa_select

        existing = await session.execute(
            sa_select(ProductivityEvent.id)
            .where(ProductivityEvent.idempotency_key == body.idempotency_key)
            .limit(1)
        )
        prior = existing.scalar_one_or_none()
        if prior is not None:
            return {"status": "ok", "event_id": str(prior), "deduplicated": True}

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
        idempotency_key=body.idempotency_key,
        created_at=datetime.now(timezone.utc),
    )

    if body.idempotency_key:
        from sqlalchemy.exc import IntegrityError

        try:
            async with session.begin_nested():
                session.add(event)
            await session.commit()
        except IntegrityError:
            # 경합에서 졌다 = 상대가 같은 키로 먼저 적재했다. 세이브포인트가 되감겨
            # 세션은 살아 있으므로, 승자의 id 를 찾아 같은 응답을 돌려준다.
            await session.rollback()
            from sqlalchemy import select as sa_select

            winner = await session.execute(
                sa_select(ProductivityEvent.id)
                .where(ProductivityEvent.idempotency_key == body.idempotency_key)
                .limit(1)
            )
            wid = winner.scalar_one_or_none()
            return {
                "status": "ok",
                "event_id": str(wid) if wid else "duplicate",
                "deduplicated": True,
            }
    else:
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
