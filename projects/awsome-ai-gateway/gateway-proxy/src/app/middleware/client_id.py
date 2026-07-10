# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Client identification middleware (pure ASGI).

Classifies each HTTP request as claude-code / cowork / other from its headers
and stores the result in scope["state"]["client"] for downstream consumers
(cost logging now; routing in a later phase). Never raises — on any error it
defaults to "other" so identification can never break a request.

Pure ASGI (not BaseHTTPMiddleware) to stay compatible with SSE streaming —
see main.py for the rationale.
"""

from __future__ import annotations

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from app.services.client_identifier import CLIENT_OTHER, identify_client

logger = structlog.get_logger(__name__)


class ClientIdentificationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        try:
            headers = {
                k.decode("latin-1"): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            state["client"] = identify_client(headers)
        except Exception:
            logger.warning("client_identification_failed")
            state["client"] = CLIENT_OTHER

        await self.app(scope, receive, send)
