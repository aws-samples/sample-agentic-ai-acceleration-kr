# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Integration test fixtures.

Uses real FastAPI test client with mocked DB/Redis for router-level testing.
Full DB integration tests use testcontainers (requires Docker).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.auth import CurrentUser, JWTVerifier
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.encryption import AESEncryptionService
from app.models.auth import UserRole
from app.services.analytics_service import AnalyticsService
from app.services.budget_service import BudgetService
from app.services.cli_service import CLIService
from app.services.key_service import KeyService
from app.services.model_service import ModelService
from app.services.rate_limit_service import RateLimitService
from app.services.user_team_service import UserTeamService

TEST_ENCRYPTION_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

# JWT test keypair (RS256)
# In real integration tests, generate an RSA key pair. For router tests, we mock the verifier.
ADMIN_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ADMIN_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
LEADER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
DEV_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _build_test_app() -> FastAPI:
    """Build a FastAPI app with mocked dependencies for integration tests."""
    from app.routers import analytics, budgets, cli, internal, keys, models, rate_limits, users
    from app.core.exceptions import (
        AppError, NotFoundError, ConflictError, ForbiddenError,
        ValidationError, BudgetExceededError, STSVerificationError,
    )
    from fastapi import Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Test Admin API")

    # Exception handlers (same as main.py)
    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"error": {"message": exc.message, "code": exc.code}})

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"error": {"message": exc.message, "code": exc.code}})

    @app.exception_handler(ForbiddenError)
    async def forbidden_handler(request: Request, exc: ForbiddenError):
        return JSONResponse(status_code=403, content={"error": {"message": exc.message, "code": exc.code}})

    @app.exception_handler(ValidationError)
    async def validation_handler(request: Request, exc: ValidationError):
        return JSONResponse(status_code=400, content={"error": {"message": exc.message, "code": exc.code}})

    @app.exception_handler(BudgetExceededError)
    async def budget_handler(request: Request, exc: BudgetExceededError):
        return JSONResponse(status_code=429, content={"error": {"message": exc.message, "code": exc.code}})

    @app.exception_handler(STSVerificationError)
    async def sts_handler(request: Request, exc: STSVerificationError):
        return JSONResponse(status_code=401, content={"error": {"message": exc.message, "code": exc.code}})

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(status_code=500, content={"error": {"message": exc.message, "code": exc.code}})

    app.include_router(keys.router)
    app.include_router(budgets.router)
    app.include_router(models.router)
    app.include_router(rate_limits.router)
    app.include_router(users.router)
    app.include_router(analytics.router)
    app.include_router(cli.router)
    app.include_router(internal.router)

    return app


class MockJWTVerifier(JWTVerifier):
    """Always returns a predetermined payload based on the token value."""

    def verify(self, token: str) -> dict:
        if token == "admin-token":
            return {"sub": str(ADMIN_USER_ID), "email": "admin@test.com", "role": "ADMIN", "team_id": str(ADMIN_TEAM_ID)}
        elif token == "leader-token":
            return {"sub": str(LEADER_USER_ID), "email": "leader@test.com", "role": "TEAM_LEADER", "team_id": str(ADMIN_TEAM_ID)}
        elif token == "dev-token":
            return {"sub": str(DEV_USER_ID), "email": "dev@test.com", "role": "DEVELOPER", "team_id": str(ADMIN_TEAM_ID)}
        else:
            from jose import JWTError
            raise JWTError("Invalid token")


@pytest.fixture
def test_app() -> FastAPI:
    app = _build_test_app()

    # Mock dependencies
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.delete = AsyncMock()
    mock_redis.ping = AsyncMock()

    encryption = AESEncryptionService(TEST_ENCRYPTION_KEY)
    cache_mgr = CacheInvalidationManager(mock_redis)

    app.state.redis = mock_redis
    app.state.cache_mgr = cache_mgr
    app.state.jwt_verifier = MockJWTVerifier()

    key_service = KeyService(encryption=encryption, cache_mgr=cache_mgr)
    app.state.key_service = key_service
    app.state.cli_service = CLIService(key_service=key_service)
    app.state.budget_service = BudgetService(cache_mgr=cache_mgr)
    app.state.model_service = ModelService(cache_mgr=cache_mgr)
    app.state.rate_limit_service = RateLimitService(cache_mgr=cache_mgr)
    app.state.user_team_service = UserTeamService(cache_mgr=cache_mgr, key_service=key_service)
    app.state.analytics_service = AnalyticsService()

    return app


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-token"}


@pytest.fixture
def leader_headers() -> dict[str, str]:
    return {"Authorization": "Bearer leader-token"}


@pytest.fixture
def dev_headers() -> dict[str, str]:
    return {"Authorization": "Bearer dev-token"}
