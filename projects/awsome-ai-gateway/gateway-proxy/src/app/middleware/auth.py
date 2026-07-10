# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.schemas.domain import AuthType
from app.services.auth_service import resolve_auth_strategy

logger = structlog.get_logger(__name__)

EXEMPT_PATHS = {"/health", "/health/ready"}


class AuthMiddleware:
    """경로 기반 인증 전략 선택 + 실행 Middleware (Pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        state = scope.setdefault("state", {})
        degradation_manager = state.get("degradation_manager") or scope.get("app", {})

        # 인증 면제 경로
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        strategy = resolve_auth_strategy(path)
        if strategy is None:
            await self._send_401(scope, send, "Unknown route")
            return

        # request 객체에서 Authorization 헤더 추출
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode()

        dm = state.get("_degradation_manager")
        redis = state.get("_redis")
        session_factory = state.get("_session_factory")

        # Degradation 상태에 따라 DB/Redis 가용 여부 결정
        degradation_level = None
        if dm is not None:
            degradation_level = dm.level

        from app.schemas.domain import DegradationLevel

        if degradation_level == DegradationLevel.BOTH_DEGRADED:
            await self._send_503(scope, send)
            return

        effective_redis = (
            redis if (degradation_level != DegradationLevel.REDIS_DEGRADED) else None
        )
        use_db = (
            degradation_level != DegradationLevel.DB_DEGRADED and session_factory is not None
        )

        try:
            if use_db:
                # short-lived session: VK 인증 쿼리만 수행 후 즉시 반환.
                # SSE 응답 동안 pool 을 점유하지 않아 pool 회전율 크게 개선.
                async with session_factory() as db:
                    auth_context = await strategy.authenticate(
                        authorization, effective_redis, db
                    )
            else:
                auth_context = await strategy.authenticate(
                    authorization, effective_redis, None
                )
            state["auth_context"] = auth_context
            structlog.contextvars.bind_contextvars(
                user_id=auth_context.user_id,
                sso_subject=auth_context.sso_subject,
            )
            await self.app(scope, receive, send)

        except (PermissionError, ValueError) as e:
            error_msg = str(e)
            logger.info("auth_failed", path=path, reason=error_msg)

            # 보안 이벤트 감지
            sec_detector = state.get("_security_detector")
            if sec_detector is not None:
                client = dict(scope.get("headers", [])).get(b"x-forwarded-for", b"")
                source_ip = client.decode() if client else "unknown"
                auth_type = AuthType.JWT if path.startswith("/v1/") else AuthType.VIRTUAL_KEY
                import asyncio

                asyncio.create_task(sec_detector.record_auth_failure(source_ip, auth_type))

            await self._send_401(scope, send, error_msg)

    async def _send_401(self, scope: Scope, send: Send, detail: str) -> None:
        body = json.dumps(
            {
                "error": {
                    "type": "authentication_error",
                    "message": "Unauthorized",
                    "code": "auth_failed",
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _send_503(self, scope: Scope, send: Send) -> None:
        body = json.dumps(
            {"error": {"type": "service_unavailable", "message": "Service temporarily unavailable"}}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
