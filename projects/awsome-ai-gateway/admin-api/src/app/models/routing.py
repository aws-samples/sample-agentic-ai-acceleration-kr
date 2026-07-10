# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""model.routing_profiles — admin-api read/write mirror of the gateway-proxy ORM.

gateway-proxy owns the read path (RoutingProfileLoader, Redis-cached). admin-api
only needs to read/update the per-client `web_search_enabled` toggle from the admin
UI. Columns mirror gateway-proxy/src/app/models/routing.py; we only touch the flag.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class RoutingProfile(Base):
    __tablename__ = "routing_profiles"
    __table_args__ = {"schema": "model"}

    client: Mapped[str] = mapped_column(String(64), primary_key=True)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    account_role_arn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    default_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    web_search_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
