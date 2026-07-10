# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from datetime import datetime
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import Boolean, Date, DateTime, Enum, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class UsageStatus(str, PyEnum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


class UsageRecord(Base):
    __tablename__ = "usage_logs"
    __table_args__ = {"schema": "usage"}

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)
    dept_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    model_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[UsageStatus] = mapped_column(
        Enum(UsageStatus, name="usage_status", schema="usage", create_type=False),
        nullable=False,
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_streaming: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    estimated_usage: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    downgraded_from: Mapped[str | None] = mapped_column(String(128), nullable=True)


class DailyAggregate(Base):
    """`usage.daily_aggregates` — scheduler가 전일 usage_logs를 집계해서 채움.

    Granularity: (date, user_id, model_alias). FR-4a.2 `/v1/usage/me`의 과거
    일자 breakdown 데이터 소스. scheduler 잡: admin-api/scheduler/daily_usage_aggregator.py.
    """

    __tablename__ = "daily_aggregates"
    __table_args__ = {"schema": "usage"}

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    date: Mapped[datetime] = mapped_column(Date, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)
    dept_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    model_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
