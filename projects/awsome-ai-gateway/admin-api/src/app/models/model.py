# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class Provider(str, enum.Enum):
    BEDROCK = "BEDROCK"
    OPENMODEL = "OPENMODEL"
    BEDROCK_MANTLE = "BEDROCK_MANTLE"  # Cowork → 905 Bedrock Mantle (Tokyo Opus 4.8)
    BEDROCK_MANTLE_OPENAI = "BEDROCK_MANTLE_OPENAI"  # Codex → 859 Bedrock Mantle GPT-5.5 (Ohio, Responses)


class ApiFormat(str, enum.Enum):
    BEDROCK_NATIVE = "BEDROCK_NATIVE"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"
    ANTHROPIC_MESSAGES = "ANTHROPIC_MESSAGES"  # Mantle /anthropic/v1/messages
    OPENAI_RESPONSES = "OPENAI_RESPONSES"  # Mantle /openai/v1/responses (GPT-5.x)


class ModelStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class RateLimitScope(str, enum.Enum):
    USER = "USER"
    TEAM = "TEAM"
    GLOBAL = "GLOBAL"


# ── ModelAlias ──


class ModelAlias(Base):
    __tablename__ = "model_aliases"
    __table_args__ = {"schema": "model"}

    alias: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[Provider] = mapped_column(Enum(Provider, name="provider", schema="model", create_type=False), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(512), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    api_format: Mapped[ApiFormat] = mapped_column(Enum(ApiFormat, name="api_format", schema="model", create_type=False), nullable=False)
    status: Mapped[ModelStatus] = mapped_column(
        Enum(ModelStatus, name="model_status", schema="model", create_type=False), nullable=False, default=ModelStatus.ACTIVE
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    pricings: Mapped[list[ModelPricing]] = relationship(back_populates="model", lazy="selectin")


# ── ModelPricing ──


class ModelPricing(Base):
    __tablename__ = "model_pricings"
    __table_args__ = {"schema": "model"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_alias: Mapped[str] = mapped_column(
        String(128), ForeignKey("model.model_aliases.alias"), nullable=False
    )
    input_price_per_1k_tokens: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    output_price_per_1k_tokens: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    cache_creation_5m_price_per_1k_tokens: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    cache_creation_1h_price_per_1k_tokens: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    cache_read_price_per_1k_tokens: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=False)

    model: Mapped[ModelAlias] = relationship(back_populates="pricings", lazy="selectin")


# ── TeamAllowedModel ──


class TeamAllowedModel(Base):
    """Team-scoped model whitelist.

    빈 엔트리 (해당 team_id 행 없음) = 전체 허용.
    엔트리 존재 = 해당 model_alias만 허용.
    VK 발급 시 AuthContext.allowed_models 스냅샷 주입.
    """

    __tablename__ = "team_allowed_models"
    __table_args__ = {"schema": "model"}

    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("auth.teams.id", ondelete="CASCADE"), primary_key=True
    )
    model_alias: Mapped[str] = mapped_column(
        String(128), ForeignKey("model.model_aliases.alias"), primary_key=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("auth.users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── UserAllowedModel ──


class UserAllowedModel(Base):
    """User-scoped model whitelist — OVERRIDES the user's team policy.

    우선순위(스냅샷 시점 해결): user > team > none.
      행 존재 = 이 화이트리스트만 허용(팀 무시).
      행 0개  = team_allowed_models 로 폴백(override 해제). ★ "전체 허용"이 아님.
    국가핵심기술 등 동일 팀 내 개별 인원 제한용. (260626_comm_customer 항목2)
    """

    __tablename__ = "user_allowed_models"
    __table_args__ = {"schema": "model"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("auth.users.id", ondelete="CASCADE"), primary_key=True
    )
    model_alias: Mapped[str] = mapped_column(
        String(128), ForeignKey("model.model_aliases.alias"), primary_key=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("auth.users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── RateLimitConfig ──


class RateLimitConfig(Base):
    __tablename__ = "rate_limit_configs"
    __table_args__ = {"schema": "model"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope: Mapped[RateLimitScope] = mapped_column(Enum(RateLimitScope, name="rate_limit_scope", schema="model", create_type=False), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    model_alias: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("model.model_aliases.alias"), nullable=True
    )
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpm_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    cph_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
