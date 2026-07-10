# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OpenAI-compatible + JWT OAuth configuration (LP-03, SP-01, RP-02, US-02)."""

from __future__ import annotations

import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import requests
import structlog

from cli.config import (
    ComponentResult,
    ComponentStatus,
    DetectedTool,
    GatewayConfig,
    SetupComponentType,
    ToolType,
)
from cli.utils.config_rw import atomic_write_json, read_json

log = structlog.get_logger(component="cli")

OAUTH_TIMEOUT = 120  # seconds (BR-JWT-03)


# ---------------------------------------------------------------------------
# OAuth callback handler (SP-01 — closure-based state)
# ---------------------------------------------------------------------------

def create_oauth_handler(expected_state: str):
    """Factory that captures OAuth state in closure for CSRF validation (SP-01)."""
    result = {"code": None, "error": None}

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            state = qs.get("state", [None])[0]
            code = qs.get("code", [None])[0]

            if state != expected_state:
                result["error"] = "CSRF verification failed: state mismatch"
                self._respond(400, "Authentication failed.")
                return

            if not code:
                result["error"] = "No authorization code received"
                self._respond(400, "Authentication failed.")
                return

            result["code"] = code
            self._respond(200, "Authentication successful. You can close this window.")

        def _respond(self, status: int, message: str):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(message.encode())

        def log_message(self, format, *args):
            pass  # Suppress default stderr logging (use structlog)

    return OAuthCallbackHandler, result


# ---------------------------------------------------------------------------
# OAuth flow (RP-02 — simple lifecycle)
# ---------------------------------------------------------------------------

def _run_oauth_flow(config: GatewayConfig) -> str:
    """Execute OAuth authorization code flow and return JWT token.

    Raises RuntimeError on failure.
    """
    jwt_auth = config.jwt_auth
    sso_auth_url = jwt_auth.get("sso_auth_url", "")
    sso_token_url = jwt_auth.get("sso_token_url", "")
    client_id = jwt_auth.get("client_id", "gateway-cli")
    scope = jwt_auth.get("scope", "openid profile")

    if not sso_auth_url or not sso_token_url:
        raise RuntimeError("JWT SSO configuration missing (sso_auth_url, sso_token_url)")

    state = secrets.token_urlsafe(32)
    handler_cls, result = create_oauth_handler(state)

    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    redirect_uri = f"http://localhost:{port}/callback"
    server.timeout = OAUTH_TIMEOUT

    auth_url = (
        f"{sso_auth_url}"
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
        f"&scope={scope}"
    )

    log.info("oauth_browser_open", auth_url=sso_auth_url)
    webbrowser.open(auth_url)

    try:
        server.handle_request()  # Single request only (RP-02)
    finally:
        server.server_close()

    if result["error"]:
        raise RuntimeError(result["error"])
    if not result["code"]:
        raise RuntimeError("Authentication timeout — no callback received within 120s")

    # Token exchange
    resp = requests.post(
        sso_token_url,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        },
        timeout=(config.connect_timeout, config.read_timeout),
    )
    resp.raise_for_status()
    token_data = resp.json()
    jwt_token = token_data.get("access_token", "")
    if not jwt_token:
        raise RuntimeError("Token exchange succeeded but no access_token in response")

    return jwt_token


# ---------------------------------------------------------------------------
# Per-tool config apply (LP-03)
# ---------------------------------------------------------------------------

def apply_opencode_config(original: dict, gateway_url: str, jwt_token: str) -> dict:
    """Merge JWT settings into OpenCode config.json."""
    updated = dict(original)
    updated.setdefault("provider", {})
    updated["provider"]["endpoint"] = f"{gateway_url}/v1"
    updated["provider"]["api_key"] = jwt_token
    return updated


def apply_cline_config(original: dict, gateway_url: str, jwt_token: str) -> dict:
    """Merge JWT settings into Cline (VS Code settings)."""
    updated = dict(original)
    updated["cline.apiProvider"] = "openai-compatible"
    updated["cline.openaiBaseUrl"] = f"{gateway_url}/v1"
    updated["cline.openaiApiKey"] = jwt_token
    return updated


_APPLY_FUNCS = {
    ToolType.OPENCODE: apply_opencode_config,
    ToolType.CLINE: apply_cline_config,
}


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def setup_jwt(tool: DetectedTool, config: GatewayConfig) -> ComponentResult:
    """Set up JWT authentication for OpenAI-compatible tools (US-02)."""
    if not config.jwt_auth:
        return ComponentResult(
            component=SetupComponentType.JWT_AUTH,
            status=ComponentStatus.SKIPPED,
            message="JWT SSO config missing. Add jwt_auth section to config.yaml.",
        )

    try:
        jwt_token = _run_oauth_flow(config)
    except Exception as exc:
        log.error("jwt_auth_failed", tool=tool.name, error=str(exc))
        return ComponentResult(
            component=SetupComponentType.JWT_AUTH,
            status=ComponentStatus.FAILED,
            message="JWT authentication failed",
            error=str(exc),
        )

    apply_fn = _APPLY_FUNCS.get(tool.tool_type)
    if not apply_fn:
        return ComponentResult(
            component=SetupComponentType.JWT_AUTH,
            status=ComponentStatus.SKIPPED,
            message=f"No JWT config handler for {tool.name}",
        )

    try:
        original = read_json(tool.config_path)
    except ValueError as exc:
        return ComponentResult(
            component=SetupComponentType.JWT_AUTH,
            status=ComponentStatus.FAILED,
            message="Config file parse error",
            error=str(exc),
        )

    updated = apply_fn(original, config.gateway_url, jwt_token)
    atomic_write_json(tool.config_path, updated)

    log.info("jwt_configured", tool=tool.name)
    return ComponentResult(
        component=SetupComponentType.JWT_AUTH,
        status=ComponentStatus.SUCCESS,
        message=f"JWT token configured for {tool.name}",
    )
