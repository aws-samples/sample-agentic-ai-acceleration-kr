# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base

# Postgres native enum types (already created by migrations; create_type=False)
_provider_enum = Enum(
    "BEDROCK",
    "OPENMODEL",
    "BEDROCK_MANTLE",
    "BEDROCK_MANTLE_OPENAI",  # Codex → 859 Mantle GPT-5.5 (migration 0016)
    # GPT-5.6 on the STANDARD bedrock-runtime plane, SigV4 + CRIS (migration 0031).
    # ⚠️ This list must contain EVERY label in the Postgres enum, not just the ones this
    # service dispatches on. SQLAlchemy validates on READ: a row whose provider is not
    # listed here raises LookupError while fetching, so a single unlisted label breaks
    # every model lookup and the whole /admin/models listing — not just that one row.
    "BEDROCK_RUNTIME_OPENAI",
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
    #: 모델 × 앱 축의 allow-list (migration 0035). 3-state 의미는
    #: ``services/router_service.check_client_model_scope`` 의 docstring 이 정본이다:
    #:   NULL      제한 없음(나중에 추가되는 앱까지 포함)
    #:   {}        명시적 빈 allow-list = **어떤 앱도 이 모델을 쓸 수 없다**
    #:   {codex}   그 앱만 허용
    #:
    #: ⚠️ 이 줄이 없으면 게이트가 **조용히 무력화된다.** `_orm_to_schema` 는
    #:    `getattr(alias_row, "allowed_clients", None)` 로 읽는데, ORM 이 컬럼을
    #:    선언하지 않으면 그 getattr 이 언제나 None 을 돌려주고
    #:    `check_client_model_scope` 는 즉시 return 한다 — 관리자 화면은 "이 앱 차단"
    #:    이라고 표시하고 admin-api 도 저장에 성공하는데, 데이터 경로에서는 모든 앱이
    #:    그 모델을 계속 호출한다. 실제로 그 상태로 배포된 적이 있다(회귀 테스트가
    #:    `_orm_to_schema` 의 kwarg 이름만 AST 로 확인해서 통과했다).
    #:
    #: ⚠️ 그래서 `db/init/02_create_tables.sql` 에도 idempotent ALTER 가 있어야 한다.
    #:    이 컬럼은 model_aliases 의 모든 SELECT 에 들어가므로, 0035 를 적용하지 않은
    #:    DB(init SQL 로만 만든 compose/로컬)에서는 **모든 모델 조회가** UndefinedColumn
    #:    으로 죽어 추론 경로 전체가 500 이 된다.
    allowed_clients: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), nullable=True, default=None
    )
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
