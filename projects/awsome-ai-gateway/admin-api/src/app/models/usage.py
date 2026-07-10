# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class UsageStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


class ROIScope(str, enum.Enum):
    USER = "USER"
    TEAM = "TEAM"
    DEPT = "DEPT"
    GLOBAL = "GLOBAL"


class ProductivityEventType(str, enum.Enum):
    CODE_GENERATED = "CODE_GENERATED"
    CODE_ACCEPTED = "CODE_ACCEPTED"
    CODE_REJECTED = "CODE_REJECTED"


class GitEventType(str, enum.Enum):
    COMMIT = "COMMIT"
    PR_OPENED = "PR_OPENED"
    PR_MERGED = "PR_MERGED"


# ── UsageLog (Admin API: READ ONLY, Gateway Proxy writes) ──


class UsageLog(Base):
    __tablename__ = "usage_logs"
    __table_args__ = {"schema": "usage"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.teams.id"), nullable=False)
    dept_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.departments.id"), nullable=False)
    model_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Reasoning/thinking tokens (migration 0019) — visibility submetric, NOT billed
    # separately (GPT-5.x already counts it inside output_tokens). Written by the
    # cost-recorder-worker; read-only here for analytics/UI across all 3 clients.
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Server-side web search calls (migration 0020) — attribution metric, NOT billed
    # (not a token count). Written by the cost-recorder-worker; read-only here to
    # attribute AgentCore WebSearch usage ($7/1k) per client on the dashboard.
    web_search_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[UsageStatus] = mapped_column(Enum(UsageStatus, name="usage_status", schema="usage", create_type=False), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_streaming: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    estimated_usage: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    downgraded_from: Mapped[str | None] = mapped_column(String(128), nullable=True)
    availability_fallback_from: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Phase 2 client tag (claude-code | cowork | other | NULL legacy). Written by
    # gateway-proxy; read-only here. DB column is `text` (migration 0007) — use
    # unbounded String() to match it and avoid an autogenerate-truncation footgun.
    client: Mapped[str | None] = mapped_column(String(), nullable=True)


# ── ROIAggregation ──


class ROIAggregation(Base):
    __tablename__ = "roi_aggregations"
    __table_args__ = {"schema": "usage"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    scope: Mapped[ROIScope] = mapped_column(Enum(ROIScope, name="roi_scope", schema="usage", create_type=False), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    # Cost metrics
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=Decimal("0"))
    cost_per_user_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("0"))
    budget_utilization_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("0"))
    cost_by_model: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Activity metrics
    active_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_user_rate_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("0"))
    requests_per_user_per_day: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, default=Decimal("0"))
    activation_gap_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("0"))

    # Productivity metrics (Post-MVP, nullable)
    code_acceptance_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    cost_per_accepted_code_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    generated_lines_per_session: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)

    # ROI index (Post-MVP, nullable)
    roi_index: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)

    aggregated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    aggregated_by: Mapped[str] = mapped_column(String(64), nullable=False)


# ── ProductivityEvent (CLI 코드 생성/수락 이벤트) ──


class ProductivityEvent(Base):
    __tablename__ = "productivity_events"
    __table_args__ = {"schema": "usage"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("auth.teams.id"), nullable=True)
    event_type: Mapped[ProductivityEventType] = mapped_column(
        Enum(ProductivityEventType, name="productivity_event_type", schema="usage", create_type=False),
        nullable=False,
    )
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_alias: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lines_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── GitEvent (GitHub/GitLab webhook 커밋/PR 이벤트) ──


class GitEvent(Base):
    __tablename__ = "git_events"
    __table_args__ = {"schema": "usage"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=True)
    user_email: Mapped[str] = mapped_column(String(320), nullable=False)
    event_type: Mapped[GitEventType] = mapped_column(
        Enum(GitEventType, name="git_event_type", schema="usage", create_type=False),
        nullable=False,
    )
    repo: Mapped[str] = mapped_column(String(512), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    commit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
