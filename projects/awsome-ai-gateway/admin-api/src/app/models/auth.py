# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.models.base import Base


class UserRole(str, enum.Enum):
    ADMIN = "ADMIN"
    TEAM_LEADER = "TEAM_LEADER"
    DEVELOPER = "DEVELOPER"


class KeyStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class RotationScope(str, enum.Enum):
    GLOBAL = "GLOBAL"
    TEAM = "TEAM"
    USER = "USER"


# ── Organization ──


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = {"schema": "auth"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    departments: Mapped[list[Department]] = relationship(back_populates="organization", lazy="selectin")


# ── Department ──


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = {"schema": "auth"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="departments", lazy="selectin")
    teams: Mapped[list[Team]] = relationship(back_populates="department", lazy="selectin")


# ── Team ──


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = {"schema": "auth"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dept_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.departments.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    leader_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("auth.users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    department: Mapped[Department] = relationship(back_populates="teams", lazy="selectin")
    members: Mapped[list[User]] = relationship(
        back_populates="team", foreign_keys="User.team_id", lazy="selectin"
    )


# ── User ──


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "auth"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("auth.teams.id"), nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role", schema="auth", create_type=False), nullable=False, default=UserRole.DEVELOPER)
    sso_subject: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    # Auth origin: 'sts' (legacy) or 'oidc:<idp>' (예: 'oidc:keycloak').
    # 마이그레이션 0002 에서 NOT NULL DEFAULT 'sts' 로 추가. 다중 IDP 동시 운영 시 식별자.
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="sts", server_default="sts")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    team: Mapped[Team | None] = relationship(back_populates="members", foreign_keys=[team_id], lazy="selectin")
    virtual_keys: Mapped[list[VirtualKey]] = relationship(
        back_populates="user", foreign_keys="VirtualKey.user_id", lazy="selectin"
    )


# ── VirtualKey ──


class VirtualKey(Base):
    __tablename__ = "virtual_keys"
    __table_args__ = {"schema": "auth"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=False)
    key_value_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[KeyStatus] = mapped_column(
        Enum(KeyStatus, name="key_status", schema="auth", create_type=False), nullable=False, default=KeyStatus.ACTIVE
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="virtual_keys", foreign_keys=[user_id], lazy="selectin")


# ── RotationPolicy ──


class RotationPolicy(Base):
    __tablename__ = "rotation_policies"
    __table_args__ = {"schema": "auth"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope: Mapped[RotationScope] = mapped_column(Enum(RotationScope, name="rotation_scope", schema="auth", create_type=False), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    expiry_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_days_before: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, default=[7, 1])
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── AdminJWTConfig ──


class AdminJWTConfig(Base):
    __tablename__ = "admin_jwt_configs"
    __table_args__ = {"schema": "auth"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    audience: Mapped[str] = mapped_column(String(512), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(16), nullable=False, default="RS256")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── UserAllowedClient ──


class UserAllowedClient(Base):
    __tablename__ = "user_allowed_clients"
    __table_args__ = {"schema": "auth"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("auth.users.id", ondelete="CASCADE"), primary_key=True
    )
    client: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── ServiceToken ──


class ServiceToken(Base):
    __tablename__ = "service_tokens"
    __table_args__ = {"schema": "auth"}

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("auth.users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_from: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("auth.service_tokens.id"), nullable=True
    )
