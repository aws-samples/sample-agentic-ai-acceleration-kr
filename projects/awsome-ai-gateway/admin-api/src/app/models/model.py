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
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class Provider(str, enum.Enum):
    BEDROCK = "BEDROCK"
    OPENMODEL = "OPENMODEL"
    BEDROCK_MANTLE = "BEDROCK_MANTLE"  # Cowork → 905 Bedrock Mantle (Tokyo Opus 4.8)
    BEDROCK_MANTLE_OPENAI = "BEDROCK_MANTLE_OPENAI"  # Codex → 859 Bedrock Mantle GPT-5.5 (Ohio, Responses)
    # GPT-5.6 on the STANDARD bedrock-runtime plane: SigV4 instead of a bearer token, and
    # a cross-region inference-profile model id (us./global.openai.gpt-5.6-*). Same OpenAI
    # dialect as BEDROCK_MANTLE_OPENAI, so api_format stays OPENAI_RESPONSES — the plane is
    # what `provider` names. Added by migration 0031.
    #
    # ⚠️ Must list EVERY label in the Postgres enum: SQLAlchemy validates on READ, so an
    # unlisted label raises LookupError while fetching and breaks the entire model listing,
    # not merely the row that uses it.
    BEDROCK_RUNTIME_OPENAI = "BEDROCK_RUNTIME_OPENAI"


class ApiFormat(str, enum.Enum):
    BEDROCK_NATIVE = "BEDROCK_NATIVE"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"
    ANTHROPIC_MESSAGES = "ANTHROPIC_MESSAGES"  # Mantle /anthropic/v1/messages
    # Both OpenAI planes (Mantle and bedrock-runtime). Names the DIALECT, not the endpoint;
    # runtime-plane rows also serve /v1/chat/completions on the same dialect.
    OPENAI_RESPONSES = "OPENAI_RESPONSES"


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
    #: 이 모델을 쓸 수 있는 앱(client) 허용목록 — **모델 × 앱** 인가 축(migration 0035).
    #:
    #: 3-상태 의미론(이 필드의 유일한 정의, 전 계층 동일):
    #:   ``None``  미설정 = 제한 없음. 지금 앱과 나중에 추가될 앱 전부 허용.
    #:   ``[]``    명시적으로 빈 허용목록 = **어떤 앱도 이 모델을 쓸 수 없다.**
    #:   non-empty 허용목록. 목록 밖 client 는 거부되며 ``other``/``None`` 도 거부.
    #:
    #: ⚠️ ``[]`` 를 "전부 허용" 으로 읽으면 안 된다. ``[]`` 는 운영자가 콘솔에서 마지막
    #:    앱의 체크를 해제했을 때 만들어지는 값이라, 화면은 "허용된 앱 없음" 으로
    #:    보여주는데 게이트가 전부 통과시키는 상태가 된다(원본 구현의 실제 결함).
    #:    판정은 반드시 ``is None`` 으로 — falsiness 는 두 상태를 섞는다.
    #:
    #: ``auth.user_allowed_clients``(사용자 × 앱)와는 **다른 축**이고 AND 로 걸린다.
    allowed_clients: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
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
    # long-context 단가 티어 (마이그레이션 0038). threshold NULL = 티어 없음. 명시 요율 컬럼
    # (배수 아님) — AWS Price List 자동연동이 fetch 한 달러 요율을 그대로 넣기 위함.
    long_context_threshold_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    long_context_input_price_per_1k_tokens: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    long_context_output_price_per_1k_tokens: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    long_context_cache_creation_5m_price_per_1k_tokens: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    long_context_cache_creation_1h_price_per_1k_tokens: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    long_context_cache_read_price_per_1k_tokens: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6), nullable=True
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
