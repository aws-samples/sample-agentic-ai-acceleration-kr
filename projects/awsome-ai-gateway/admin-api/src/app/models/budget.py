# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class BudgetScope(str, enum.Enum):
    TEAM = "TEAM"
    USER = "USER"


class PeriodType(str, enum.Enum):
    MONTHLY = "MONTHLY"


class BudgetPolicy(str, enum.Enum):
    HARD_BLOCK = "HARD_BLOCK"
    SOFT_WARNING = "SOFT_WARNING"
    THROTTLE = "THROTTLE"


# ── BudgetConfig ──


class BudgetConfig(Base):
    __tablename__ = "budget_configs"
    __table_args__ = {"schema": "budget"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope: Mapped[BudgetScope] = mapped_column(Enum(BudgetScope, name="budget_scope", schema="budget", create_type=False), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    client: Mapped[str | None] = mapped_column(String(32), nullable=True)
    max_budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    period_type: Mapped[PeriodType] = mapped_column(
        Enum(PeriodType, name="period_type", schema="budget", create_type=False), nullable=False, default=PeriodType.MONTHLY
    )
    policy: Mapped[BudgetPolicy] = mapped_column(
        Enum(BudgetPolicy, name="budget_policy", schema="budget", create_type=False), nullable=False, default=BudgetPolicy.HARD_BLOCK
    )
    allocated_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── BudgetUsage ──


class BudgetUsage(Base):
    __tablename__ = "budget_usages"
    __table_args__ = {"schema": "budget"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope: Mapped[BudgetScope] = mapped_column(Enum(BudgetScope, name="budget_scope", schema="budget", create_type=False), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    client: Mapped[str | None] = mapped_column(String(32), nullable=True)
    used_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=Decimal("0"))
    limit_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    threshold_notified_pcts: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=[]
    )


# ── DowngradePolicy ──


class DowngradePolicy(Base):
    """Budget-aware 모델 다운그레이드 매핑.

    scope + scope_id로 사용자/팀 식별. 예산 소진율이 threshold_pct 이상이면
    from_model_alias 요청을 to_model_alias로 자동 전환.

    One-hop only: to_model_alias가 또 downgrade 대상이어도 체인 follow 하지 않음.
    """

    __tablename__ = "downgrade_policies"
    __table_args__ = {"schema": "budget"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope: Mapped[BudgetScope] = mapped_column(
        Enum(BudgetScope, name="budget_scope", schema="budget", create_type=False),
        nullable=False,
    )
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False)
    from_model_alias: Mapped[str] = mapped_column(
        String(128), ForeignKey("model.model_aliases.alias"), nullable=False
    )
    to_model_alias: Mapped[str] = mapped_column(
        String(128), ForeignKey("model.model_aliases.alias"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("auth.users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
