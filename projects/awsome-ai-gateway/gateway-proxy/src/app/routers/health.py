# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.degradation.manager import DegradationManager
from app.schemas.domain import DegradationLevel
from app.schemas.responses import HealthComponent, HealthResponse

router = APIRouter()


@router.get("/health")
async def health_check(request: Request) -> JSONResponse:
    dm: DegradationManager = request.app.state.degradation_manager
    level = dm.level

    components = {
        "postgresql": HealthComponent(
            status="down"
            if level in (DegradationLevel.DB_DEGRADED, DegradationLevel.BOTH_DEGRADED)
            else "up"
        ),
        "redis": HealthComponent(
            status="down"
            if level in (DegradationLevel.REDIS_DEGRADED, DegradationLevel.BOTH_DEGRADED)
            else "up"
        ),
    }

    if level == DegradationLevel.BOTH_DEGRADED:
        status, http_code = "unhealthy", 503
    elif level != DegradationLevel.HEALTHY:
        status, http_code = "degraded", 200
    else:
        status, http_code = "healthy", 200

    return JSONResponse(
        status_code=http_code,
        content={
            "status": status,
            "components": {k: v.model_dump() for k, v in components.items()},
            "degradation_level": level.value,
        },
    )


@router.get("/health/ready")
async def readiness_check(request: Request) -> JSONResponse:
    """Readiness probe — liveness(/health)와 분리한 '트래픽 수용 가능' 신호.

    핵심 차이: /health 는 DB 단독 열화(DB_DEGRADED)에도 200 을 반환해 풀 고갈 파드가
    endpoint 에 남는다(견고성 6축검증 축⑤ GAP). readiness 는 더 엄격하게:
      - DegradationLevel != HEALTHY  → 503 (강등된 파드를 로테이션에서 제외)
      - 커넥션 풀 포화(checkedout >= size+overflow) → 503 (풀 고갈 파드 배제)
    두 신호 모두 **비블로킹**(라이브 쿼리 안 함) — admin-api 의 정적 ready 처럼 boot-storm
    cascade 를 피하되, degradation gauge + pool 카운터라는 이미 계산된 상태만 읽는다.
    liveness 는 여전히 /health(관대) 라 재시작 cascade 는 유발하지 않는다.
    """
    dm: DegradationManager = request.app.state.degradation_manager
    level = dm.level

    # 풀 포화 판정 (비블로킹 — SQLAlchemy sync pool 카운터. 라이브 커넥션 획득 안 함).
    pool_saturated = False
    pool_info: dict = {}
    engine = getattr(request.app.state, "db_engine", None)
    if engine is not None:
        try:
            pool = engine.pool
            checked_out = pool.checkedout()
            regular_size = pool.size()                        # 상시 풀 크기(예: 20)
            # max_overflow 는 settings 에서 읽는다 — pool._max_overflow(사설 속성) 의존을 피해,
            # 향후 SQLAlchemy 리네임으로 hard_cap 이 조용히 regular_size 로 붕괴(HPA min=1 에
            # 위험한 조기 트립)하는 것을 방지(리뷰 LOW). fallback 은 사설속성.
            _settings = get_settings()
            max_overflow = getattr(
                _settings, "db_max_overflow", getattr(pool, "_max_overflow", 0)
            )
            hard_cap = regular_size + max_overflow            # 이론상 최대(예: 30)
            # 포화 판정은 **hard_cap 소진** 기준(보수적). 이유: 이 배포는 HPA min=1 이라
            # readiness 를 "바쁨(regular_size 초과)"에 걸면 부하 스파이크 때 유일 파드(또는
            # 전 파드 동시)가 endpoint 에서 빠져 **자초 outage**가 된다(readiness 는 재시작도 안 함).
            # 진짜 지속 고갈은 아래 DegradationLevel 게이트가 이미 잡는다 — HealthChecker 의
            # SELECT 1 이 같은 풀을 쓰므로 pool_timeout(10s) 내 실패→DB_DEGRADED→503. 이 카운터
            # 체크는 hard_cap 소진(모든 슬롯 점유=명백한 고갈, 순간 blip 아님)만 빠르게 잡는
            # 보조 신호다. transient 부하(hard_cap 미만)엔 파드를 빼지 않는다.
            if hard_cap > 0 and checked_out >= hard_cap:
                pool_saturated = True
            pool_info = {
                "checked_out": checked_out,
                "regular_size": regular_size,
                "max_overflow": max_overflow,
                "hard_cap": hard_cap,
                "overflow_open": max(0, pool.overflow()),  # 실제 열린 overflow 수(음수 정규화)
            }
        except Exception:
            # pool introspection 실패는 readiness 를 떨어뜨릴 사유가 아님(관대).
            pool_info = {"error": "pool_introspection_failed"}

    ready = level == DegradationLevel.HEALTHY and not pool_saturated
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "degradation_level": level.value,
            "pool_saturated": pool_saturated,
            "pool": pool_info,
        },
    )
