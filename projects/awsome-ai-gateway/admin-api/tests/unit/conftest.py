# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Shared fixtures for unit tests.

Unit tests mock all external dependencies (DB, Redis) and test service logic in isolation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.encryption import AESEncryptionService
from app.models.auth import UserRole

# Deterministic encryption key for tests (64-char hex = 32 bytes)
TEST_ENCRYPTION_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.fixture
def admin_user() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="admin@test.com",
        role=UserRole.ADMIN,
        team_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
    )


@pytest.fixture
def team_leader_user() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        email="leader@test.com",
        role=UserRole.TEAM_LEADER,
        team_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
    )


@pytest.fixture
def developer_user() -> CurrentUser:
    return CurrentUser(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        email="dev@test.com",
        role=UserRole.DEVELOPER,
        team_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
    )


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.delete = AsyncMock()
    redis.ping = AsyncMock()

    # scan_iter is an async generator; default to empty unless test overrides.
    async def _empty_scan(*_args, **_kwargs):
        return
        yield  # pragma: no cover — make this an async generator

    redis.scan_iter = MagicMock(side_effect=lambda *a, **kw: _empty_scan())
    return redis


@pytest.fixture
def cache_mgr(mock_redis: AsyncMock) -> CacheInvalidationManager:
    return CacheInvalidationManager(mock_redis)


@pytest.fixture
def encryption() -> AESEncryptionService:
    return AESEncryptionService(TEST_ENCRYPTION_KEY)


@pytest.fixture
def db_session_factory():
    """Mock async_sessionmaker for unit tests of audit batch queue.

    The factory returns a context manager yielding a session whose `add_all` calls
    are recorded so tests can assert batch INSERT behavior. We do NOT use a real DB
    here because AuditLog uses Postgres-specific schema=audit and JSONB types.
    Real DB integration is covered separately (Finch e2e in Task A5).
    """
    from contextlib import asynccontextmanager

    inserted: list = []  # captured AuditLog instances across all flushes

    @asynccontextmanager
    async def _session_cm():
        session = MagicMock()
        session.add_all = MagicMock(side_effect=lambda items: inserted.extend(items))
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        yield session

    factory = MagicMock(side_effect=_session_cm)
    factory.inserted = inserted  # expose for test assertions
    return factory
