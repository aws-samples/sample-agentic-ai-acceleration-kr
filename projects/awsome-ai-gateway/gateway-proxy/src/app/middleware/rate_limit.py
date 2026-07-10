# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Rate Limit Middleware — Redis-down In-Memory Fallback 전담.

주 rate limit enforcement는 라우터의 enforce_rate_limits()가 담당 (모델 정보
해결 이후 full 3-scope RPM + TPM 체크). 이 미들웨어는 **Redis 장애 상황**에서만
USER 스코프 RPM을 in-memory로 근사 체크해 남용을 최소화.
"""

from __future__ import annotations

import json

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.services.rate_limit_scope import GLOBAL_WILDCARD, RateLimitScope, build_rl_key
from app.services.rate_limit_service import InMemoryRateLimiter

logger = structlog.get_logger(__name__)

# Redis-down fallback 의 in-memory USER RPM 카운터는 **프로세스마다 독립**이라
# 함대 전체엔 `replicas × uvicorn_workers` 개가 존재한다. divisor 를 그 곱으로
# 잡아야 각 카운터가 `limit // (replicas × workers)` 를 허용해 클러스터 총합이
# limit 에 맞는다(과거 하드코딩 4=uvicorn_workers 만, HPA replica 무시 → 부하테스트
# 429×6 배경, deepdive Q46/Q50). rl_fallback_replicas 기본 1 = 과거 동작 보존.
_settings = get_settings()
_fallback_worker_count = max(1, _settings.rl_fallback_replicas) * max(1, _settings.uvicorn_workers)
_in_memory_limiter = InMemoryRateLimiter(worker_count=_fallback_worker_count)

# Redis-down fallback 기본값 (Step 5 DB 로드 반영 시 덮어쓰기). Redis 다운 시 DB
# 한도 조회도 못 하므로 보수적 상수. USER < TEAM < GLOBAL 로 계층 한도(deepdive Q50
# Phase3 — 과거엔 USER 만 검사해 한 팀이 fallback 용량을 독점할 수 있었다).
_FALLBACK_USER_RPM = 60
_FALLBACK_TEAM_RPM = 600
_FALLBACK_GLOBAL_RPM = 6000


class RateLimitMiddleware:
    """Redis-down 시 USER 스코프 RPM In-Memory Fallback (Pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        state = scope.setdefault("state", {})

        is_rate_limited_path = path.startswith("/model/") or path in (
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/messages",
            "/v1/responses",  # Codex (Responses API) must be rate-limited (incl. degraded mode)
        )
        if not is_rate_limited_path:
            await self.app(scope, receive, send)
            return

        auth_context = state.get("auth_context")
        if auth_context is None:
            await self.app(scope, receive, send)
            return

        from app.schemas.domain import DegradationLevel

        dm = state.get("_degradation_manager")
        is_redis_degraded = dm and dm.level in (
            DegradationLevel.REDIS_DEGRADED,
            DegradationLevel.BOTH_DEGRADED,
        )
        redis = state.get("_redis")

        # Redis 정상 → 라우터의 enforce_rate_limits 가 처리
        if redis is not None and not is_redis_degraded:
            await self.app(scope, receive, send)
            return

        # Redis 다운 → In-Memory Fallback. USER→TEAM→GLOBAL 계층 모두 근사 집행
        # (deepdive Q50 Phase3 — 과거 USER 만 검사해 한 팀이 fallback 용량 독점 가능).
        model_config = state.get("model_config")
        model_alias = (
            (model_config.alias or model_config.provider_model_id)
            if model_config
            else "unknown"
        )

        # Budget throttle 비율(있으면) 적용 — USER 한도에만(예산은 사용자 귀속).
        throttle_pct = None
        budget_status = state.get("budget_status")
        if budget_status and budget_status.throttle_active:
            throttle_pct = budget_status.throttle_rpm_pct or 50

        user_rpm = _FALLBACK_USER_RPM
        if throttle_pct is not None:
            user_rpm = max(1, int(user_rpm * throttle_pct / 100))

        # (scope, scope_id, limit) — USER→TEAM→GLOBAL fast-fail 순서.
        checks = [
            (RateLimitScope.USER, auth_context.user_id, user_rpm),
            (RateLimitScope.TEAM, auth_context.team_id, _FALLBACK_TEAM_RPM),
            (RateLimitScope.GLOBAL, GLOBAL_WILDCARD, _FALLBACK_GLOBAL_RPM),
        ]

        metrics = getattr(getattr(scope.get("app", None), "state", None), "metrics", None)
        if metrics:
            metrics.rl_fallback_entered_total.add(1)

        for sc, sid, limit in checks:
            if not sid:  # team_id 누락 등 → 해당 scope 스킵
                continue
            key = build_rl_key(sc, sid, model_alias, "rpm")
            result = _in_memory_limiter.check(key, limit)
            if not result.allowed:
                state["rate_limit_result"] = result
                if metrics:
                    metrics.rl_fallback_429_total.add(1, {"scope": sc.value})
                await self._send_429_fallback(scope, send, result, sc.value)
                return
            state["rate_limit_result"] = result  # 마지막 통과 결과 노출

        await self.app(scope, receive, send)

    async def _send_429_fallback(
        self, scope: Scope, send: Send, result, scope_name: str = "USER"
    ) -> None:
        body = json.dumps(
            {
                "error": {
                    "type": "rate_limit_error",
                    "message": (
                        f"Rate limit exceeded (degraded mode). "
                        f"Please retry after {result.retry_after} seconds."
                    ),
                    "code": f"{scope_name.lower()}_rpm_exceeded",
                    "scope": scope_name,
                    "limit_type": "rpm",
                    "retry_after": result.retry_after,
                    "degraded": True,
                }
            }
        ).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"retry-after", str(result.retry_after or 60).encode()),
            (b"x-ratelimit-scope", scope_name.encode()),
            (b"x-ratelimit-type", b"rpm"),
            (b"x-ratelimit-remaining", b"0"),
            (b"x-ratelimit-limit", str(result.limit).encode()),
            (b"x-ratelimit-reset", str(result.window_reset).encode()),
            (b"x-ratelimit-degraded", b"true"),
        ]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})
