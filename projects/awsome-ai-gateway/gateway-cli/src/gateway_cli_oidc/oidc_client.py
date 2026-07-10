# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OIDC PKCE Authorization Code flow + token cache + admin-api VK exchange.

Generic OIDC — config-driven (issuer_url, client_id, audience). Works with any
OIDC-compliant IDP. Local browser-based login: PKCE (RFC 7636), localhost
callback server, refresh token rotation, secure file cache (mode 0600).
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
import structlog

log = structlog.get_logger(component="oidc-client")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class OIDCConfig:
    """OIDC client config. Populated from env vars or YAML."""

    issuer_url: str
    client_id: str
    audience: str = ""  # 일부 IDP 는 audience 가 client_id 와 동일 — 비워둘 수 있음
    redirect_port: int = 8090
    # Cognito App Client 의 allowed_oauth_scopes 와 호환되도록 offline_access 제외.
    # Cognito 는 refresh_token_validity 설정 만으로 refresh token 발급.
    # Keycloak / Okta 등 표준 IDP 도 이 3개 scope 로 refresh 동작.
    # IDP 가 추가 scope 요구하면 OIDC_SCOPES env 로 override.
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    admin_api_url: str = ""  # POST /v1/auth/exchange 호출용


def load_oidc_config_from_env() -> OIDCConfig | None:
    """환경변수에서 OIDC 설정 로드. issuer_url + client_id 가 모두 있어야 활성."""
    issuer = os.environ.get("OIDC_ISSUER_URL", "").rstrip("/")
    client_id = os.environ.get("OIDC_CLIENT_ID", "")
    if not issuer or not client_id:
        return None

    # OIDC_SCOPES env (공백 구분) — IDP 별 scope 차이 흡수.
    scopes_env = os.environ.get("OIDC_SCOPES", "").strip()
    scopes = tuple(scopes_env.split()) if scopes_env else ("openid", "profile", "email")

    return OIDCConfig(
        issuer_url=issuer,
        client_id=client_id,
        audience=os.environ.get("OIDC_AUDIENCE", ""),
        redirect_port=int(os.environ.get("OIDC_REDIRECT_PORT", "8090")),
        scopes=scopes,
        admin_api_url=(os.environ.get("ADMIN_API_URL") or os.environ.get("GATEWAY_CLI_GATEWAY_URL") or "").rstrip("/"),
    )


# ---------------------------------------------------------------------------
# OIDC discovery
# ---------------------------------------------------------------------------

class OIDCDiscoveryError(RuntimeError):
    pass


def discover(issuer_url: str, timeout: float = 5.0) -> dict:
    """``.well-known/openid-configuration`` fetch."""
    url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise OIDCDiscoveryError(f"discovery failed for {issuer_url}: {e}") from e


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _gen_code_verifier() -> str:
    """RFC 7636: 43-128 chars from [A-Z][a-z][0-9]-._~."""
    return _b64url(secrets.token_bytes(64))[:96]


def _gen_code_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

class _CallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    result: _CallbackResult  # set by serve_once

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        self.result.code = (params.get("code") or [None])[0]
        self.result.state = (params.get("state") or [None])[0]
        self.result.error = (params.get("error") or [None])[0]
        self.result.error_description = (params.get("error_description") or [None])[0]

        body = (
            b"<html><body style='font-family:sans-serif;padding:2em;'>"
            b"<h2>Login complete</h2>"
            b"<p>You can close this window and return to the terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence default access log


def _serve_once_for_callback(port: int, timeout_seconds: int = 300) -> _CallbackResult:
    """localhost:port 에서 /callback 한 번 받고 종료."""
    result = _CallbackResult()
    handler = type("H", (_CallbackHandler,), {"result": result})

    server = HTTPServer(("127.0.0.1", port), handler)
    server.timeout = 1  # poll interval

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        server.handle_request()
        if result.code or result.error:
            break
    server.server_close()
    return result


# ---------------------------------------------------------------------------
# Token store (~/.gateway-cli/oidc-tokens.json, mode 0600)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds (monotonic-ish, IDP exp 기반)
    issuer_url: str
    client_id: str
    # id_token 은 사용자 신원 (email, name, groups) 을 담고 있어 admin-api 의
    # 자동 프로비저닝에 사용. Cognito 는 access_token 에 email 미포함이라
    # id_token 이 필수. OIDC 표준상으로도 신원 정보 = id_token.
    id_token: str = ""

    def is_expiring(self, threshold_seconds: int = 60) -> bool:
        return time.time() + threshold_seconds >= self.expires_at

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Tokens":
        return cls(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token"),
            expires_at=float(d["expires_at"]),
            issuer_url=d["issuer_url"],
            client_id=d["client_id"],
            id_token=d.get("id_token", ""),
        )


def _token_cache_path() -> Path:
    override = os.environ.get("GATEWAY_CLI_OIDC_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".gateway-cli" / "oidc-tokens.json"


def load_tokens() -> Tokens | None:
    path = _token_cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Tokens.from_dict(data)
    except (OSError, ValueError, KeyError):
        return None


def save_tokens(tokens: Tokens) -> None:
    path = _token_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens.to_dict()), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def clear_tokens() -> None:
    path = _token_cache_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# VK cache (~/.gateway-cli/vk-cache.json, mode 0600)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CachedVK:
    """admin-api 가 발급한 VK 의 client-side 캐시."""

    virtual_key: str
    expires_at: float           # epoch seconds
    issuer_url: str             # 발급 시 IDP (다중 IDP 분리)
    admin_api_url: str
    user_id: str = ""
    team_id: str = ""

    def is_expiring(self, threshold_seconds: int = 300) -> bool:
        return time.time() + threshold_seconds >= self.expires_at

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CachedVK":
        return cls(
            virtual_key=d["virtual_key"],
            expires_at=float(d["expires_at"]),
            issuer_url=d.get("issuer_url", ""),
            admin_api_url=d.get("admin_api_url", ""),
            user_id=d.get("user_id", ""),
            team_id=d.get("team_id", ""),
        )


def _vk_cache_path() -> Path:
    override = os.environ.get("GATEWAY_CLI_VK_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".gateway-cli" / "vk-cache.json"


def load_vk_cache() -> CachedVK | None:
    path = _vk_cache_path()
    if not path.exists():
        return None
    try:
        return CachedVK.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None


def save_vk_cache(vk: CachedVK) -> None:
    path = _vk_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(vk.to_dict()), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def clear_vk_cache() -> None:
    path = _vk_cache_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Login (PKCE Authorization Code flow)
# ---------------------------------------------------------------------------

class OIDCLoginError(RuntimeError):
    pass


def _is_port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def login_pkce(config: OIDCConfig, timeout_seconds: int = 300) -> Tokens:
    """Browser PKCE flow → access + refresh token. 토큰 캐시 자동 저장."""
    if not _is_port_free(config.redirect_port):
        raise OIDCLoginError(
            f"redirect port {config.redirect_port} is busy. "
            "Close the conflicting app or set OIDC_REDIRECT_PORT."
        )

    discovery = discover(config.issuer_url)
    auth_endpoint = discovery["authorization_endpoint"]
    token_endpoint = discovery["token_endpoint"]

    code_verifier = _gen_code_verifier()
    code_challenge = _gen_code_challenge(code_verifier)
    state = _b64url(secrets.token_bytes(32))
    redirect_uri = f"http://localhost:{config.redirect_port}/callback"

    # Authorization request
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(config.scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"{auth_endpoint}?{qs}"

    print(f"Opening browser for OIDC login...\n  {auth_url}\n", file=sys.stderr)
    print(
        "If the browser doesn't open automatically, copy the URL above into your browser.\n",
        file=sys.stderr,
    )

    # Browser open — non-fatal if it fails (user can paste URL manually)
    try:
        webbrowser.open(auth_url)
    except webbrowser.Error:
        pass

    # Wait for callback
    result_holder: dict = {}
    def _serve():
        result_holder["result"] = _serve_once_for_callback(config.redirect_port, timeout_seconds)
    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    t.join(timeout_seconds + 5)

    cb = result_holder.get("result")
    if cb is None or (cb.code is None and cb.error is None):
        raise OIDCLoginError(f"login timed out after {timeout_seconds}s")
    if cb.error:
        raise OIDCLoginError(f"IDP returned error: {cb.error} ({cb.error_description})")
    if cb.state != state:
        raise OIDCLoginError("CSRF check failed: state mismatch")
    assert cb.code is not None

    # Token exchange
    resp = requests.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": cb.code,
            "redirect_uri": redirect_uri,
            "client_id": config.client_id,
            "code_verifier": code_verifier,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise OIDCLoginError(f"token exchange failed: HTTP {resp.status_code} {resp.text[:200]}")
    body = resp.json()
    tokens = Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=time.time() + int(body.get("expires_in", 3600)),
        issuer_url=config.issuer_url,
        client_id=config.client_id,
        id_token=body.get("id_token", ""),
    )
    save_tokens(tokens)
    return tokens


def refresh_tokens(config: OIDCConfig, tokens: Tokens) -> Tokens:
    """refresh_token 으로 access_token 갱신 + 새 refresh_token 캐시."""
    if not tokens.refresh_token:
        raise OIDCLoginError("no refresh_token available — re-login required")

    discovery = discover(config.issuer_url)
    resp = requests.post(
        discovery["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": config.client_id,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise OIDCLoginError(f"refresh failed: HTTP {resp.status_code} {resp.text[:200]}")
    body = resp.json()
    new_tokens = Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token") or tokens.refresh_token,
        expires_at=time.time() + int(body.get("expires_in", 3600)),
        issuer_url=tokens.issuer_url,
        client_id=tokens.client_id,
        id_token=body.get("id_token", tokens.id_token),
    )
    save_tokens(new_tokens)
    return new_tokens


def get_valid_access_token(config: OIDCConfig) -> str:
    """Return a non-expired access token. Auto-refresh if needed."""
    return _get_valid_tokens(config).access_token


def get_valid_id_token(config: OIDCConfig) -> str:
    """Return a non-expired id_token (사용자 신원 claim 포함).

    admin-api 의 자동 프로비저닝에 사용. Cognito 는 access_token 에 email 없어
    id_token 이 필수. 다른 표준 OIDC IDP 에서도 동일.
    """
    return _get_valid_tokens(config).id_token


def _get_valid_tokens(config: OIDCConfig) -> Tokens:
    tokens = load_tokens()
    if tokens is None:
        raise OIDCLoginError("not logged in. Run: gateway-cli login")
    if tokens.issuer_url != config.issuer_url or tokens.client_id != config.client_id:
        raise OIDCLoginError("cached tokens belong to a different IDP — re-login needed")
    if tokens.is_expiring(threshold_seconds=60):
        log.info("oidc.refreshing_access_token")
        tokens = refresh_tokens(config, tokens)
    return tokens


# ---------------------------------------------------------------------------
# admin-api JWT → VK exchange
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class VKResponse:
    virtual_key: str
    expires_at: datetime
    user_id: str = ""
    team_id: str = ""


class OIDCExchangeError(RuntimeError):
    pass


def exchange_jwt_for_vk(
    admin_api_url: str,
    access_token: str,
    device_name: str,
    timeout: tuple[int, int] = (5, 15),
) -> VKResponse:
    if not admin_api_url:
        raise OIDCExchangeError("ADMIN_API_URL not set")
    url = f"{admin_api_url.rstrip('/')}/v1/auth/exchange"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"device_name": device_name},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise OIDCExchangeError(
            f"exchange failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
    data = resp.json()
    expires_at_str = data.get("expires_at", "")
    try:
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        expires_at = datetime.now(timezone.utc)
    return VKResponse(
        virtual_key=data["virtual_key"],
        expires_at=expires_at,
        user_id=data.get("user_id", ""),
        team_id=data.get("team_id", "") or "",
    )
