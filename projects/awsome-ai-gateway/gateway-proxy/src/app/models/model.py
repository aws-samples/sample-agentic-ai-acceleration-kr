# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base

# Postgres native enum types (already created by migrations; create_type=False)
_provider_enum = Enum(
    "BEDROCK",
    "OPENMODEL",
    "BEDROCK_MANTLE",
    "BEDROCK_MANTLE_OPENAI",  # Codex → 859 Mantle GPT-5.5 (migration 0016)
    name="provider",
    schema="model",
    create_type=False,
)
_api_format_enum = Enum(
    "BEDROCK_NATIVE",
    "OPENAI_COMPATIBLE",
    "ANTHROPIC_MESSAGES",
    "OPENAI_RESPONSES",  # Mantle /openai/v1/responses (migration 0016)
    name="api_format",
    schema="model",
    create_type=False,
)
_model_status_enum = Enum(
    "ACTIVE",
    "INACTIVE",
    name="model_status",
    schema="model",
    create_type=False,
)
_rate_limit_scope_enum = Enum(
    "USER",
    "TEAM",
    "GLOBAL",
    name="rate_limit_scope",
    schema="model",
    create_type=False,
)


class ModelAlias(Base):
    """`model.model_aliases` 테이블 매핑.

    PK는 alias (사람이 읽는 짧은 이름). provider_model_id는 Bedrock/HuggingFace 등의
    실제 식별자. 양쪽 모두로 RouterService가 조회 가능.
    """

    __tablename__ = "model_aliases"
    __table_args__ = {"schema": "model"}

    alias: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(_provider_enum, nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(512), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    api_format: Mapped[str] = mapped_column(_api_format_enum, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(_model_status_enum, nullable=False, default="ACTIVE")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TeamAllowedModel(Base):
    """`model.team_allowed_models` 매핑 .

    팀 엔트리 0개 → 전체 허용.
    엔트리 존재 → 화이트리스트 enforcement.
    """

    __tablename__ = "team_allowed_models"
    __table_args__ = {"schema": "model"}

    team_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("auth.teams.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_alias: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("model.model_aliases.alias"),
        primary_key=True,
    )
    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserAllowedModel(Base):
    """`model.user_allowed_models` 매핑 (read-only, gateway 스냅샷용).

    우선순위: user > team > none. 행 존재 → 이 화이트리스트만 허용(팀 무시),
    행 0개 → team_allowed_models 로 폴백. (auth_service VK fallback 에서 조회)
    """

    __tablename__ = "user_allowed_models"
    __table_args__ = {"schema": "model"}

    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("auth.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_alias: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("model.model_aliases.alias"),
        primary_key=True,
    )
    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelPricing(Base):
    """`model.model_pricings` 테이블 매핑.

    한 alias에 여러 pricing 레코드 가능 (시계열). 가장 최근 effective_from + 만료
    안 됨 조건의 레코드를 선택.
    """

    __tablename__ = "model_pricings"
    __table_args__ = {"schema": "model"}

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    model_alias: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("model.model_aliases.alias"),
        nullable=False,
        index=True,
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
    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)


class RateLimitConfig(Base):
    """`model.rate_limit_configs` 테이블 매핑.

    스코프 + 모델 alias 조합으로 한도 설정. ``model_alias`` NULL이면 해당 스코프의
    **전체 모델 합산 한도** (예: USER 사용자 X의 모든 모델 합산 RPM).
    GLOBAL 스코프는 ``scope_id IS NULL + model_alias`` 지정 → Bedrock 쿼터 방어.

    ``cpm_limit_usd``/``cph_limit_usd``는 비용 기반 rate limit용.
    """

    __tablename__ = "rate_limit_configs"
    __table_args__ = {"schema": "model"}

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )
    scope: Mapped[str] = mapped_column(_rate_limit_scope_enum, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    model_alias: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("model.model_aliases.alias"),
        nullable=True,
    )
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpm_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    cph_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
