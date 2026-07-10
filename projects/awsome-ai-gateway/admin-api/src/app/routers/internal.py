# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db_session

router = APIRouter(tags=["Internal"])


@router.post("/internal/cache/retry")
async def retry_cache_invalidation(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Retry all unresolved cache invalidation failures."""
    from app.core.cache_invalidation import CacheInvalidationManager

    cache_mgr: CacheInvalidationManager = request.app.state.cache_mgr
    resolved = await cache_mgr.retry_failed(session)
    return {"resolved": resolved}


@router.post("/internal/scheduler/run")
async def trigger_aggregation(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Manual trigger for ROI aggregation (testing/debug only)."""
    settings = get_settings()
    if settings.APP_ENV == "production":
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Not available in production")

    from datetime import datetime, timezone

    from app.scheduler.roi_aggregator import aggregate_usage

    period = datetime.now(timezone.utc).strftime("%Y-%m")
    await aggregate_usage(session, period)
    return {"status": "ok", "period": period}


@router.post("/internal/test/issue-key")
async def test_issue_key(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """[DEV ONLY] Issue a VK without STS auth — for integration testing."""
    settings = get_settings()
    if settings.APP_ENV == "production":
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Not available in production")

    import uuid
    from datetime import date
    from decimal import Decimal

    from app.core.auth import CurrentUser
    from app.models.auth import User, UserRole
    from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope, PeriodType
    from app.repositories.budget_repository import BudgetRepository
    from app.repositories.user_repository import UserRepository
    from app.services.key_service import KeyService

    body = await request.json()
    email = body.get("email", "test@example.com")
    display_name = body.get("display_name", "Test User")
    budget_usd = body.get("budget_usd", settings.DEFAULT_USER_BUDGET_USD)
    expires_seconds = body.get("expires_seconds")  # optional: short-lived VK for testing

    # Find or create user
    user_repo = UserRepository(session)
    user = await user_repo.get_by_email(email)

    if user is None:
        team_id = uuid.UUID(settings.DEFAULT_TEAM_ID) if settings.DEFAULT_TEAM_ID else None
        # Bedrock metadata.user_id 가 이메일 형태를 거부하므로 UUID5 로 변환.
        # 동일 email → 동일 sub (재실행 idempotent), prod OIDC `sub` 와 동일한 UUID 형태.
        sso_subject = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"local-test:{email}"))
        user = User(
            id=uuid.uuid4(),
            email=email,
            display_name=display_name,
            role=UserRole.DEVELOPER,
            sso_subject=sso_subject,
            team_id=team_id,
            is_active=True,
        )
        user = await user_repo.create_user(user)

        # Create budget
        if budget_usd > 0 and team_id:
            budget_repo = BudgetRepository(session)
            budget = BudgetConfig(
                id=uuid.uuid4(),
                scope=BudgetScope.USER,
                scope_id=user.id,
                max_budget_usd=Decimal(str(budget_usd)),
                period_type=PeriodType.MONTHLY,
                policy=BudgetPolicy.HARD_BLOCK,
                allocated_by=user.id,
                effective_from=date.today(),
                is_active=True,
            )
            await budget_repo.upsert_config(budget)

    # Issue VK
    actor = CurrentUser(
        user_id=user.id,
        email=user.email,
        role=user.role,
        team_id=user.team_id,
    )
    key_service: KeyService = request.app.state.key_service
    key_result = await key_service.issue_key(
        session,
        user_id=user.id,
        actor=actor,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_seconds) if expires_seconds else None,
        ip_address=request.client.host if request.client else "0.0.0.0",
        request_id=request.headers.get("x-request-id", ""),
    )

    # Cache budget config in Redis
    redis = request.app.state.redis
    budget_repo = BudgetRepository(session)
    user_budget = await budget_repo.get_active_config(BudgetScope.USER, user.id)
    if user_budget:
        import json

        await redis.set(
            f"budget:config:user:{{{user.id}}}",
            json.dumps({
                "limit_usd": str(user_budget.max_budget_usd),
                "policy": user_budget.policy.value.lower(),
                "soft_limit_pct": 110,
                "throttle_rpm_pct": 50,
                "thresholds": [80, 90, 100],
            }),
        )

    # NOTE: 모델 config Redis 캐시는 gateway-proxy 의 router_service 가 DB 에서
    # 동적 로드/관리. 여기서 미리 쓰던 fallback 은 옛 스키마 (model_id / BEDROCK / active)
    # 라 gateway-proxy 가 ModelConfigSchema validation 실패 → 401. 제거.

    return {
        "virtual_key": key_result.virtual_key,
        "key_id": key_result.key_id,
        "user_id": str(user.id),
        "team_id": str(user.team_id) if user.team_id else None,
        "email": user.email,
        "budget_usd": str(user_budget.max_budget_usd) if user_budget else "0",
        "expires_at": key_result.expires_at.isoformat() if key_result.expires_at else None,
        "gateway_endpoint": "http://localhost:8000",
    }


@router.get("/health")
async def health_check():
    """Liveness — 프로세스 alive 여부만 확인 (의존성 검사 X).

    DB/Redis 등 외부 의존성이 순간 느려지면 probe timeout → pod kill → cascade.
    pod 재시작으로 외부 의존성 장애가 해결되지 않으므로 liveness 엔 부적절.
    외부 의존성 상태는 /health/ready (readinessProbe 용) 에서 확인.
    """
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness_check():
    """Readiness — 프로세스 alive 여부만 확인 (의존성 검사 X).

    이전 구현은 매 probe 마다 DB `SELECT 1` + Redis ping 을 실행했는데,
    10k 동접 boot storm 부하테스트 (t3-vk-issue-10k-v2) 에서 15 pod 이
    **동시에 readiness probe timeout → 전원 not-ready → Service endpoint
    텅 빔 → 새 요청 전부 dial timeout** 의 cascade 가 관찰됨.

    gateway-proxy 가 이전에 같은 이유로 degradation 기준을 완화한 전례
    (`values-eks-fargate-prod.yaml` 의 C2 fix) 와 동일 맥락. DB/Redis 가
    실제로 죽으면 요청이 500 을 돌려주므로 k8s 수준의 빠른 failover 가
    오히려 burst 에 해로움.
    """
    return {"status": "ready"}
