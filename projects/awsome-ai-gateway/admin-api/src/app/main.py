# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.cache_invalidation import CacheInvalidationManager
from app.core.config import get_settings
from app.core.db import AsyncSessionLocal, create_engine
from app.core.encryption import AESEncryptionService
from app.core.exceptions import (
    AppError,
    BudgetExceededError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    STSVerificationError,
    ValidationError,
)
from app.core.redis_client import create_redis_client

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("app.starting", env=settings.APP_ENV)

    # ── Initialize resources ──

    # DB engine (module-level, but ensure it's created)
    engine = create_engine()

    # Audit logger — start in-process batch consumer (A: reliable batch queue)
    from app.core.audit import audit_logger
    from app.core.db import AsyncSessionLocal as _audit_session_factory
    await audit_logger.start(_audit_session_factory)
    logger.info("audit_logger.started", batch_size=100, flush_interval_s=0.5)

    # Redis
    redis = await create_redis_client()
    app.state.redis = redis

    # Encryption — fail-fast if key is missing/invalid
    try:
        encryption = AESEncryptionService(
            settings.VIRTUAL_KEY_ENCRYPTION_KEY.get_secret_value()
        )
    except ValueError as exc:
        logger.error(
            "app.startup_failed",
            reason="VIRTUAL_KEY_ENCRYPTION_KEY invalid or missing",
            detail=str(exc),
            hint="Set a 64-char hex value. Generate via: openssl rand -hex 32",
        )
        raise

    # Cache invalidation manager
    cache_mgr = CacheInvalidationManager(redis)
    app.state.cache_mgr = cache_mgr

    # JWT Verifier — load public keys from DB
    from app.core.auth import JWTVerifier

    jwt_verifier = JWTVerifier()
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        from app.models.auth import AdminJWTConfig

        result = await session.execute(
            select(AdminJWTConfig).where(AdminJWTConfig.is_active.is_(True))
        )
        configs = result.scalars().all()
        jwt_verifier.load_configs([
            {
                "id": c.id,
                "public_key_pem": c.public_key_pem,
                "issuer": c.issuer,
                "audience": c.audience,
                "algorithm": c.algorithm,
            }
            for c in configs
        ])
    app.state.jwt_verifier = jwt_verifier

    # ── Build services ──
    from app.services.analytics_service import AnalyticsService
    from app.services.budget_service import BudgetService
    from app.services.cli_service import CLIService
    from app.services.key_service import KeyService
    from app.services.model_service import ModelService
    from app.services.rate_limit_service import RateLimitService
    from app.services.service_token_service import ServiceTokenService
    from app.services.team_allowed_model_service import TeamAllowedModelService
    from app.services.user_team_service import UserTeamService

    key_service = KeyService(encryption=encryption, cache_mgr=cache_mgr)
    app.state.key_service = key_service
    app.state.cli_service = CLIService(key_service=key_service)
    budget_service = BudgetService(cache_mgr=cache_mgr)
    app.state.budget_service = budget_service
    app.state.model_service = ModelService(cache_mgr=cache_mgr)
    app.state.rate_limit_service = RateLimitService(cache_mgr=cache_mgr)
    app.state.user_team_service = UserTeamService(cache_mgr=cache_mgr, key_service=key_service)
    app.state.team_allowed_model_service = TeamAllowedModelService(cache_mgr=cache_mgr)
    app.state.analytics_service = AnalyticsService()
    app.state.service_token_service = ServiceTokenService()

    # ── OIDC Service (optional — only if OIDC_ISSUER_URL configured) ──
    # OIDC_AUDIENCE 는 optional (Cognito access_token 은 aud claim 없음).
    if settings.OIDC_ISSUER_URL:
        import httpx as _httpx
        from app.core.oidc_verifier import OIDCVerifier
        from app.services.oidc_service import OIDCService

        oidc_http = _httpx.AsyncClient(timeout=10.0)
        oidc_verifier = OIDCVerifier(
            issuer_url=settings.OIDC_ISSUER_URL,
            audience=settings.OIDC_AUDIENCE,
            jwks_cache_ttl_seconds=settings.OIDC_JWKS_CACHE_TTL_SECONDS,
            discovery_url_override=settings.OIDC_DISCOVERY_URL_OVERRIDE or None,
        )
        app.state.oidc_http = oidc_http
        app.state.oidc_verifier = oidc_verifier
        # Cognito access_token 에는 email claim 이 없어, email 누락 시 AdminGetUser 로
        # enrich 하기 위한 client (best-effort). 풀 미구성 시 None.
        oidc_cognito_client = None
        if settings.COGNITO_USER_POOL_ID:
            import boto3
            oidc_cognito_client = boto3.client(
                "cognito-idp", region_name=settings.COGNITO_REGION
            )
        app.state.oidc_service = OIDCService(
            verifier=oidc_verifier,
            key_service=key_service,
            user_team_service=app.state.user_team_service,
            http_client=oidc_http,
            cognito_client=oidc_cognito_client,
        )
        logger.info(
            "oidc.enabled",
            issuer=settings.OIDC_ISSUER_URL,
            audience=settings.OIDC_AUDIENCE,
            provider_name=settings.OIDC_PROVIDER_NAME,
        )
    else:
        app.state.oidc_service = None
        app.state.oidc_http = None
        app.state.oidc_verifier = None
        logger.info("oidc.disabled", reason="OIDC_ISSUER_URL or OIDC_AUDIENCE empty")

    # TEAM budget config cold-cache warmup
    # init SQL / alembic 백필로 DB에 삽입된 TEAM 예산이 Redis에 없어
    # gateway-proxy 가 team_budget_unset 429 를 반환하는 문제를 startup 시 봉합.
    try:
        async with AsyncSessionLocal() as session:
            warmup_count = await budget_service.warm_team_budget_cache(session)
            logger.info("admin_api.startup.team_budget_warmup_complete", count=warmup_count)
    except Exception as exc:
        logger.warning("admin_api.startup.team_budget_warmup_failed", error=str(exc))

    # P0-③ (MF4): detect pre-existing orphan per-app budgets (no parent USER total)
    # — these bypass the gateway hot path. Read-only WARN log for operator action;
    # the set_user_client_budget guard only prevents NEW orphans.
    try:
        async with AsyncSessionLocal() as session:
            orphan_count = await budget_service.detect_orphan_app_budgets(session)
            logger.info("admin_api.startup.orphan_app_budget_scan_complete", count=orphan_count)
    except Exception as exc:
        logger.warning("admin_api.startup.orphan_app_budget_scan_failed", error=str(exc))

    logger.info("app.started")

    yield

    # ── Cleanup ──
    if getattr(app.state, "oidc_http", None) is not None:
        await app.state.oidc_http.aclose()
    await redis.aclose()
    # Audit logger — graceful drain (max 10s)
    await audit_logger.shutdown(timeout=10.0)
    logger.info("audit_logger.stopped", queue_size=audit_logger.queue_size, dropped=audit_logger.dropped_count)
    await engine.dispose()
    logger.info("app.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="LLM Gateway — Admin API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── CORS (Admin UI) ──
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.APP_ENV == "development" else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Request ID middleware ──
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        structlog.contextvars.unbind_contextvars("request_id")
        return response

    # ── Global exception handlers ──
    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": {"type": "not_found", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(
            status_code=409,
            content={"error": {"type": "conflict", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(ForbiddenError)
    async def forbidden_handler(request: Request, exc: ForbiddenError):
        return JSONResponse(
            status_code=403,
            content={"error": {"type": "forbidden", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(ValidationError)
    async def validation_handler(request: Request, exc: ValidationError):
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "validation_error", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(BudgetExceededError)
    async def budget_handler(request: Request, exc: BudgetExceededError):
        return JSONResponse(
            status_code=429,
            content={"error": {"type": "budget_exceeded", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(STSVerificationError)
    async def sts_handler(request: Request, exc: STSVerificationError):
        return JSONResponse(
            status_code=401,
            content={"error": {"type": "sts_verification_error", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=500,
            content={"error": {"type": "internal_error", "message": exc.message, "code": exc.code}},
        )

    # ── Register routers ──
    from app.routers import analytics, budgets, cli, dashboard, internal, keys, models, monitoring, my, productivity, rate_limits, routing, service_tokens, users

    app.include_router(keys.router)
    app.include_router(budgets.router)
    app.include_router(models.router)
    app.include_router(rate_limits.router)
    app.include_router(routing.router)
    app.include_router(users.router)
    app.include_router(analytics.router)
    app.include_router(dashboard.router)
    app.include_router(my.router)
    app.include_router(monitoring.router)
    app.include_router(productivity.router)
    app.include_router(cli.router)
    app.include_router(internal.router)
    app.include_router(service_tokens.router)
    from app.routers import auth_oidc
    app.include_router(auth_oidc.router)

    from app.routers import chat_agent  # admin-chat-agent BI assistant (Phase 2)
    app.include_router(chat_agent.router)

    return app


app = create_app()
