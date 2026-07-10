# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import time
from uuid import uuid4

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app.observability import get_tracer

logger = structlog.get_logger(__name__)


class OTelMiddleware:
    """OTel 트레이싱 + request_id 생성 + 메트릭 기록 Middleware (Pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid4())
        start_time = time.monotonic()
        path = scope.get("path", "")

        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id
        scope["state"]["request_start_time"] = start_time

        log = logger.bind(request_id=request_id, path=path)

        # Get metrics from app.state (set during lifespan)
        _app_state = getattr(scope.get("app", None), "state", None)
        _metrics = getattr(_app_state, "metrics", None) if _app_state else None

        if _metrics:
            _metrics.active_connections.add(1)

        response_status = 0

        async def send_with_status(message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        except Exception:
            log.exception("unhandled_exception")
            if _metrics:
                _metrics.error_total.add(1, {"path": path, "error_type": "unhandled"})
            raise
        finally:
            duration = time.monotonic() - start_time
            if _metrics:
                _metrics.active_connections.add(-1)

                is_api = path.startswith("/v1/") or path.startswith("/model/")
                if is_api:
                    attrs = {"path": path, "status": str(response_status)}
                    _metrics.request_total.add(1, attrs)
                    _metrics.request_duration.record(duration, attrs)

                    if response_status >= 400:
                        _metrics.error_total.add(1, {"path": path, "status": str(response_status)})

                    # Record model-specific metrics from state
                    state = scope.get("state", {})
                    model_config = state.get("model_config")
                    if model_config:
                        model_attrs = {
                            "model": model_config.alias or model_config.provider_model_id
                        }
                        _metrics.model_request_duration.record(duration, model_attrs)


class HeaderInjectorMiddleware:
    """응답 시 Budget / RateLimit / Cost / Common 헤더를 주입하는 Middleware."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                state = scope.get("state", {})

                # Common
                if request_id := state.get("request_id"):
                    headers.append("X-Request-Id", request_id)

                # Budget
                if budget := state.get("budget_status"):
                    headers.append("X-Budget-Remaining", str(budget.remaining_usd))
                    headers.append("X-Budget-Used", str(budget.used_usd))
                    headers.append("X-Budget-Limit", str(budget.limit_usd))
                    if budget.policy.value == "soft_warning" and state.get("budget_soft_warning"):
                        headers.append("X-Budget-Warning", "over_budget")

                # RateLimit
                if rl := state.get("rate_limit_result"):
                    headers.append("X-RateLimit-Remaining", str(rl.remaining))
                    headers.append("X-RateLimit-Limit", str(rl.limit))
                    headers.append("X-RateLimit-Reset", str(rl.window_reset))

                # Cost Limit
                if cl := state.get("cost_limit_result"):
                    headers.append("X-CostLimit-Remaining", str(cl.cpm_remaining))

                # Model Warning
                if model_config := state.get("model_config"):
                    if (
                        hasattr(model_config, "status")
                        and model_config.status.value == "deprecated"
                    ):
                        headers.append("X-Model-Warning", "deprecated")

            await send(message)

        await self.app(scope, receive, send_wrapper)
