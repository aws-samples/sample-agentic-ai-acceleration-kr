# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.budget_service import BudgetService

logger = structlog.get_logger(__name__)

_budget_service = BudgetService()

BUDGET_EXEMPT_PATHS = {"/health", "/health/ready", "/v1/models"}


class BudgetMiddleware:
    """예산 정책 확인 Middleware (Pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        state = scope.setdefault("state", {})

        if path in BUDGET_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        auth_context = state.get("auth_context")
        if auth_context is None:
            await self.app(scope, receive, send)
            return

        is_budget_path = (
            path.startswith("/model/")
            or path.startswith("/v1/messages")
            or path.startswith("/v1/chat")
            or path.startswith("/v1/completions")
            or path.startswith("/v1/responses")  # Codex (Responses API) must be budget-gated
        )
        if not is_budget_path:
            await self.app(scope, receive, send)
            return

        redis = state.get("_redis")
        session_factory = state.get("_session_factory")
        period = datetime.now(tz=timezone.utc).strftime("%Y-%m")

        from app.schemas.domain import DegradationLevel

        dm = state.get("_degradation_manager")
        is_redis_degraded = dm and dm.level in (
            DegradationLevel.REDIS_DEGRADED,
            DegradationLevel.BOTH_DEGRADED,
        )
        is_db_degraded = dm and dm.level in (
            DegradationLevel.DB_DEGRADED,
            DegradationLevel.BOTH_DEGRADED,
        )

        effective_redis = None if is_redis_degraded else redis
        use_db = not is_db_degraded and session_factory is not None

        client = state.get("client")

        try:
            # short-lived session: budget 조회 후 즉시 반환.
            if use_db:
                async with session_factory() as db:
                    budget_status = await _budget_service.check_budget(
                        effective_redis,
                        db,
                        auth_context.user_id,
                        auth_context.team_id,
                        period,
                        client=client,
                    )
            else:
                budget_status = await _budget_service.check_budget(
                    effective_redis,
                    None,
                    auth_context.user_id,
                    auth_context.team_id,
                    period,
                    client=client,
                )
            state["budget_status"] = budget_status
            if budget_status.policy.value == "soft_warning":
                from decimal import Decimal

                # Soft Warning: used >= limit 구간
                if budget_status.used_usd >= budget_status.limit_usd:
                    state["budget_soft_warning"] = True

            await self.app(scope, receive, send)

        except PermissionError as e:
            reason = str(e)
            await self._send_429_budget(scope, send, reason)

    async def _send_429_budget(self, scope: Scope, send: Send, reason: str) -> None:
        if reason == "no_budget_assigned":
            # Q 정책 적용 후 거의 발생하지 않지만 호환 위해 유지
            message = "No budget assigned. Contact your admin."
            code = "no_budget_assigned"
        elif reason == "team_budget_unset":
            message = "팀 예산이 설정되지 않았습니다. 관리자에게 문의하세요."
            code = "team_budget_unset"
        elif reason in ("team_budget_exceeded", "user_budget_exceeded"):
            message = "Budget limit exceeded."
            code = reason
        elif reason == "hard_block":
            message = "Monthly budget exhausted. Contact your team leader or admin."
            code = "hard_block"
        elif reason == "client_budget_exceeded":
            message = "App (client) budget limit exceeded. 앱별 예산을 초과했습니다."
            code = "client_budget_exceeded"
        else:
            message = "Budget limit exceeded."
            code = reason

        body = json.dumps(
            {
                "error": {
                    "type": "budget_exceeded",
                    "message": message,
                    "code": code,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
