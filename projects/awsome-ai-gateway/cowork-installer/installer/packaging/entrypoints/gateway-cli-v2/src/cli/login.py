# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Step 3 — OIDC PKCE browser login.

Reads OIDC config (issuer_url, client_id) directly from the build-baked corporate
defaults (:mod:`cli.site_defaults`) so the end user doesn't have to set any
environment variables. The full PKCE Authorization Code flow runs locally:

  1. Discover .well-known/openid-configuration from the issuer URL.
  2. Open the browser to the authorization endpoint with a PKCE code challenge.
  3. Wait for the IDP to redirect to http://localhost:<port>/callback.
  4. Exchange the auth code for tokens at the token endpoint.
  5. Exchange the id_token for a Virtual Key via admin-api /v1/auth/exchange.
  6. Cache tokens and VK to the OS-native data directory (see cli.paths).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import click
import requests
import structlog

from cli.paths import oidc_tokens_path, vk_cache_path
from cli.platform import open_browser
from cli.site_defaults import admin_api_url, oidc_client_id, oidc_issuer_url

log = structlog.get_logger(component="cli-v2")

DEFAULT_REDIRECT_PORT = 8090
SCOPES = ("openid", "profile", "email")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LoginStepError(Exception):
    """Raised when the login step fails in a way the user must fix."""


# ---------------------------------------------------------------------------
# Token store  (OS-native data dir — see cli.paths)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float   # epoch seconds
    issuer_url: str
    client_id: str
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


@dataclasses.dataclass
class CachedVK:
    virtual_key: str
    expires_at: float       # epoch seconds
    issuer_url: str
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


def _secure_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def load_tokens() -> Tokens | None:
    path = oidc_tokens_path()
    if not path.exists():
        return None
    try:
        return Tokens.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None


def save_tokens(tokens: Tokens) -> None:
    _secure_write(oidc_tokens_path(), json.dumps(tokens.to_dict()))


def load_vk_cache() -> CachedVK | None:
    path = vk_cache_path()
    if not path.exists():
        return None
    try:
        return CachedVK.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None


def save_vk_cache(vk: CachedVK) -> None:
    _secure_write(vk_cache_path(), json.dumps(vk.to_dict()))


def clear_tokens() -> None:
    """Remove cached OIDC tokens and VK (both are invalidated on logout)."""
    for p in (oidc_tokens_path(), vk_cache_path()):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_jwt_claims(token: str) -> dict:
    """Return a JWT's payload claims as a dict (no signature verification).

    We only read a claim we already trust (the token came straight from the IDP
    over TLS and is used purely to label local telemetry), so we decode the
    payload segment without verifying the signature. Returns {} on any malformed
    input rather than raising.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64 padding
        return json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception:  # noqa: BLE001 — best-effort; a bad token just yields {}
        return {}


def resolve_login_user_id() -> str | None:
    """Return the logged-in user's identity for telemetry ``user.id``.

    Prefers the cached OIDC id_token's ``email`` claim, then falls back to
    ``preferred_username`` / ``cognito:username`` / ``sub``, then to the VK
    cache's ``user_id``. Returns None when not logged in or no usable claim is
    present, so the caller can degrade gracefully.
    """
    tokens = load_tokens()
    if tokens is not None:
        claims = _decode_jwt_claims(tokens.id_token or tokens.access_token)
        for claim in ("email", "preferred_username", "cognito:username", "sub"):
            value = claims.get(claim)
            if isinstance(value, str) and value.strip():
                return value.strip()
    vk = load_vk_cache()
    if vk is not None and vk.user_id.strip():
        return vk.user_id.strip()
    return None


def _gen_code_verifier() -> str:
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


def _html_success() -> bytes:
    return (
        b"<html><head><meta charset='utf-8'>"
        b"<style>body{font-family:sans-serif;padding:2em;max-width:480px;margin:auto}"
        b"h2{color:#1a7f37}.icon{font-size:2em;margin:0}</style></head>"
        b"<body><p class='icon'>&#10003;</p>"
        b"<h2>Login complete</h2>"
        b"<p>Authentication successful. You can close this window and return to the terminal.</p>"
        b"</body></html>"
    )


def _html_failure(reason: str) -> bytes:
    safe_b = reason.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").encode("utf-8")
    return (
        b"<html><head><meta charset='utf-8'>"
        b"<style>body{font-family:sans-serif;padding:2em;max-width:480px;margin:auto}"
        b"h2{color:#cf222e}.icon{font-size:2em;margin:0}"
        b".reason{background:#fff0f0;border:1px solid #f5a0a0;border-radius:4px;"
        b"padding:.75em 1em;margin-top:1em;font-size:.95em;word-break:break-word}"
        b"</style></head>"
        b"<body><p class='icon'>&#10007;</p>"
        b"<h2>Login failed</h2>"
        b"<div class='reason'>" + safe_b +
        b"</div>"
        b"</body></html>"
    )


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handles the IDP redirect.

    Coordination contract (set by run_login before constructing this class):
      result          — shared _CallbackResult populated in do_GET
      _code_event     — set by do_GET once IDP params are captured; unblocks main thread
      _outcome_event  — set by main thread after token+VK exchange; unblocks browser response
      _outcome_holder — list[str|None]: None = success, str = human-readable error
    """
    result: _CallbackResult
    _code_event: threading.Event
    _outcome_event: threading.Event
    _outcome_holder: list  # [None] or ["error message"]

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

        # Signal main thread that IDP params are ready.
        self._code_event.set()

        if self.result.error:
            # IDP itself rejected — no need to wait for exchange.
            self._send_html(_html_failure(f"IDP error: {self.result.error}"))
            return

        # Hold the browser connection open while the main thread performs
        # token exchange + VK provisioning check (up to 20s).
        self._outcome_event.wait(timeout=20)

        error = self._outcome_holder[0]
        self._send_html(_html_success() if error is None else _html_failure(error))

    def _send_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: ARG002
        pass  # silence default HTTP access log


def _run_callback_server(
    server: "HTTPServer",
    result: _CallbackResult,
    timeout_seconds: int,
) -> None:
    """Server loop — runs in a background thread until code/error arrives and response is sent."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        server.handle_request()
        if result.code or result.error:
            break
    server.server_close()


# ---------------------------------------------------------------------------
# Admin-API VK exchange (inline — avoids importing gateway_cli_oidc)
# ---------------------------------------------------------------------------

class _VKExchangeError(RuntimeError):
    pass


def _exchange_jwt_for_vk(
    admin_api_url: str,
    id_token: str,
    device_name: str,
    issuer_url: str = "",
) -> CachedVK:
    """POST /v1/auth/exchange and return a CachedVK ready for persistence."""
    url = f"{admin_api_url.rstrip('/')}/v1/auth/exchange"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"},
            json={"device_name": device_name},
            timeout=(5, 15),
        )
    except requests.RequestException as e:
        raise _VKExchangeError(str(e)) from e
    if resp.status_code != 200:
        raise _VKExchangeError(f"HTTP {resp.status_code} {resp.text[:300]}")

    data = resp.json()
    expires_at_str = data.get("expires_at", "")
    try:
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        expires_at = time.time() + 3600  # fallback: 1h
    return CachedVK(
        virtual_key=data["virtual_key"],
        expires_at=expires_at,
        issuer_url=issuer_url,
        admin_api_url=admin_api_url,
        user_id=data.get("user_id", ""),
        team_id=data.get("team_id", "") or "",
    )


# ---------------------------------------------------------------------------
# OIDC discovery
# ---------------------------------------------------------------------------

def _discover(issuer_url: str, timeout: float = 5.0) -> dict:
    url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise LoginStepError(f"OIDC discovery failed for {issuer_url}: {e}") from e


# ---------------------------------------------------------------------------
# Core PKCE flow
# ---------------------------------------------------------------------------

def _is_port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _provision_vk_after_login(tokens: Tokens, issuer_url: str) -> None:
    """Exchange the id_token for a Virtual Key and cache it.

    Shared by the browser (run_login) and headless (run_login_password) flows.
    Raises LoginStepError with a user-facing message on a known provisioning
    rejection. No-op when no adminApiUrl is baked into the build.
    """
    api_url = admin_api_url().rstrip("/")
    if not api_url:
        return
    device_name = socket.gethostname()
    id_token = tokens.id_token or tokens.access_token
    try:
        vk = _exchange_jwt_for_vk(api_url, id_token, device_name, issuer_url=issuer_url)
    except _VKExchangeError as exc:
        err_str = str(exc)
        if "no_matching_team_group" in err_str:
            msg = "Login failed: no group mapping found. Contact your administrator."
        elif "user_deactivated" in err_str:
            msg = "Login failed: account deactivated. Contact your administrator."
        elif "required_group_missing" in err_str:
            msg = "Login failed: required group not assigned. Contact your administrator."
        else:
            msg = f"Login failed: {err_str}"
        raise LoginStepError(msg) from exc
    save_vk_cache(vk)
    log.info("vk_cached", path=str(vk_cache_path()), expires_at=vk.expires_at)


def _cognito_region_from_issuer(issuer_url: str) -> str | None:
    """Extract the AWS region from a Cognito issuer URL.

    https://cognito-idp.ap-northeast-2.amazonaws.com/<poolId>  ->  ap-northeast-2
    Returns None when the issuer is not a Cognito user-pool URL (non-Cognito IDPs
    do not support the InitiateAuth password flow, so the caller stays browser-only).
    """
    host = urllib.parse.urlparse(issuer_url).netloc
    if not host.startswith("cognito-idp."):
        return None
    parts = host.split(".")
    # cognito-idp.<region>.amazonaws.com
    if len(parts) >= 3 and parts[0] == "cognito-idp":
        return parts[1]
    return None


def is_cognito_issuer(issuer_url: str) -> bool:
    """True when the issuer supports the headless email+password fallback."""
    return _cognito_region_from_issuer(issuer_url) is not None


def _cognito_call(region: str, target: str, payload: dict, timeout: int = 15) -> dict:
    """Unsigned POST to the Cognito IDP JSON API (InitiateAuth / RespondToAuthChallenge).

    These are unauthenticated (no SigV4) operations, so a plain HTTPS POST works —
    no AWS SDK/credentials required. TLS goes through the same OS trust store the
    rest of the CLI uses (enable_os_trust_store is called at process start).
    """
    url = f"https://cognito-idp.{region}.amazonaws.com/"
    try:
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
            },
            data=json.dumps(payload),
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise LoginStepError(f"Cognito request failed: {e}") from e

    body = resp.json() if resp.content else {}
    if resp.status_code != 200:
        # Cognito error envelope: {"__type": "...Exception", "message": "..."}
        etype = (body.get("__type") or "").split("#")[-1]
        message = body.get("message", resp.text[:200])
        if etype == "NotAuthorizedException":
            raise LoginStepError("Incorrect email or password.")
        if etype == "UserNotFoundException":
            raise LoginStepError("No account found for that email.")
        if etype == "InvalidParameterException" and "USER_PASSWORD_AUTH" in message:
            raise LoginStepError(
                "Password login is not enabled for this app client "
                "(USER_PASSWORD_AUTH). Use browser login."
            )
        raise LoginStepError(f"Cognito {etype or resp.status_code}: {message}")
    return body


def run_login_password(
    email: str | None = None,
    password: str | None = None,
) -> Tokens:
    """Headless email+password login fallback (Cognito USER_PASSWORD_AUTH).

    Mirrors run_login's outcome: obtains OIDC tokens (access/id/refresh), saves
    them, then provisions + caches a Virtual Key. Only valid for Cognito issuers.
    Handles the NEW_PASSWORD_REQUIRED challenge (admin-created / force-reset
    accounts) by prompting for a new password. Reads the OIDC issuer/client id
    directly from the build-baked corporate defaults (:mod:`cli.site_defaults`).
    """
    issuer_url = oidc_issuer_url().rstrip("/")
    client_id = oidc_client_id()
    if not issuer_url or not client_id:
        raise LoginStepError("oidcIssuerUrl or oidcClientId is not baked into this build.")

    region = _cognito_region_from_issuer(issuer_url)
    if region is None:
        raise LoginStepError(
            "email+password login is only supported for AWS Cognito IDPs. "
            "Use browser login for this issuer."
        )

    if not email:
        email = click.prompt("Email").strip()
    if not password:
        password = click.prompt("Password", hide_input=True)

    print("Authenticating (no browser)...\n")
    result = _cognito_call(region, "InitiateAuth", {
        "AuthFlow": "USER_PASSWORD_AUTH",
        "ClientId": client_id,
        "AuthParameters": {"USERNAME": email, "PASSWORD": password},
    })

    # Handle a forced-new-password challenge (admin-created / reset accounts).
    challenge = result.get("ChallengeName")
    if challenge == "NEW_PASSWORD_REQUIRED":
        print("A new password is required for this account.")
        new_pw = click.prompt("New password", hide_input=True, confirmation_prompt=True)
        result = _cognito_call(region, "RespondToAuthChallenge", {
            "ChallengeName": "NEW_PASSWORD_REQUIRED",
            "ClientId": client_id,
            "Session": result.get("Session", ""),
            "ChallengeResponses": {"USERNAME": email, "NEW_PASSWORD": new_pw},
        })
    elif challenge:
        raise LoginStepError(
            f"Login requires the '{challenge}' challenge, which the CLI cannot "
            "complete without a browser. Use browser login."
        )

    auth = result.get("AuthenticationResult")
    if not auth or "AccessToken" not in auth:
        raise LoginStepError("Cognito did not return tokens. Use browser login.")

    tokens = Tokens(
        access_token=auth["AccessToken"],
        refresh_token=auth.get("RefreshToken"),
        expires_at=time.time() + int(auth.get("ExpiresIn", 3600)),
        issuer_url=issuer_url,
        client_id=client_id,
        id_token=auth.get("IdToken", ""),
    )
    save_tokens(tokens)
    _provision_vk_after_login(tokens, issuer_url)
    log.info("login_success_password", issuer_url=issuer_url, client_id=client_id)
    return tokens


def run_login(
    redirect_port: int = DEFAULT_REDIRECT_PORT,
    timeout_seconds: int = 300,
) -> Tokens:
    """Execute the OIDC PKCE browser flow using the build-baked corporate credentials.

    Opens the browser, waits for the callback, exchanges the code for tokens,
    saves them to ~/.gateway-cli/oidc-tokens.json, and returns the Tokens object.
    Reads the OIDC issuer/client id directly from :mod:`cli.site_defaults`.
    """
    issuer_url = oidc_issuer_url().rstrip("/")
    client_id = oidc_client_id()

    if not issuer_url or not client_id:
        raise LoginStepError("oidcIssuerUrl or oidcClientId is not baked into this build.")

    if not _is_port_free(redirect_port):
        raise LoginStepError(
            f"redirect port {redirect_port} is busy. "
            "Close the conflicting process or pass --redirect-port."
        )

    discovery = _discover(issuer_url)
    auth_endpoint = discovery.get("authorization_endpoint")
    token_endpoint = discovery.get("token_endpoint")
    if not auth_endpoint or not token_endpoint:
        raise LoginStepError(
            f"OIDC discovery response missing authorization_endpoint or token_endpoint: {discovery}"
        )

    code_verifier = _gen_code_verifier()
    code_challenge = _gen_code_challenge(code_verifier)
    state = _b64url(secrets.token_bytes(32))
    redirect_uri = f"http://localhost:{redirect_port}/callback"

    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"{auth_endpoint}?{qs}"

    # Print to stdout (not stderr) so it is visible even when structlog is quiet.
    print(f"Opening browser for login...\n  {auth_url}\n")
    print("If the browser does not open automatically, copy the URL above into your browser.\n")
    open_browser(auth_url)

    # Coordination primitives shared between callback handler and this thread.
    cb_result = _CallbackResult()
    code_event = threading.Event()     # handler → main: IDP params captured
    outcome_event = threading.Event()  # main → handler: exchange result ready
    outcome_holder: list = [None]      # [None] = success, ["msg"] = failure

    handler_cls = type(
        "H",
        (_CallbackHandler,),
        {
            "result": cb_result,
            "_code_event": code_event,
            "_outcome_event": outcome_event,
            "_outcome_holder": outcome_holder,
        },
    )
    server = HTTPServer(("127.0.0.1", redirect_port), handler_cls)
    server.timeout = 1

    server_thread = threading.Thread(
        target=_run_callback_server,
        args=(server, cb_result, timeout_seconds),
        daemon=True,
    )
    server_thread.start()

    # Wait for IDP redirect to arrive.
    if not code_event.wait(timeout=timeout_seconds):
        outcome_event.set()
        server_thread.join(5)
        raise LoginStepError(f"login timed out after {timeout_seconds}s")

    if cb_result.error:
        outcome_event.set()
        server_thread.join(5)
        raise LoginStepError(f"IDP returned error: {cb_result.error} ({cb_result.error_description})")

    if cb_result.state != state:
        outcome_holder[0] = "CSRF check failed: state mismatch"
        outcome_event.set()
        server_thread.join(5)
        raise LoginStepError("CSRF check failed: state mismatch in callback")

    assert cb_result.code is not None

    try:
        resp = requests.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": cb_result.code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
            timeout=10,
        )
    except requests.RequestException as e:
        outcome_holder[0] = f"Token exchange network error: {e}"
        outcome_event.set()
        server_thread.join(5)
        raise LoginStepError(f"token exchange request failed: {e}") from e

    if resp.status_code != 200:
        msg = f"Token exchange failed (HTTP {resp.status_code})"
        outcome_holder[0] = msg
        outcome_event.set()
        server_thread.join(5)
        raise LoginStepError(f"{msg}: {resp.text[:200]}")

    body = resp.json()
    tokens = Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=time.time() + int(body.get("expires_in", 3600)),
        issuer_url=issuer_url,
        client_id=client_id,
        id_token=body.get("id_token", ""),
    )
    save_tokens(tokens)

    # VK exchange — done during login so the browser sees the real outcome and
    # the VK is immediately cached for api-key-helper to consume.
    api_url = admin_api_url().rstrip("/")
    if api_url:
        device_name = socket.gethostname()
        id_token = tokens.id_token or tokens.access_token
        try:
            vk = _exchange_jwt_for_vk(
                api_url, id_token, device_name, issuer_url=issuer_url
            )
            save_vk_cache(vk)
            log.info(
                "vk_cached",
                path=str(vk_cache_path()),
                expires_at=vk.expires_at,
            )
            # Success — outcome_holder stays None
        except _VKExchangeError as exc:
            err_str = str(exc)
            if "no_matching_team_group" in err_str:
                outcome_holder[0] = "Login failed: no group mapping found. Contact your administrator."
            elif "user_deactivated" in err_str:
                outcome_holder[0] = "Login failed: account deactivated. Contact your administrator."
            elif "required_group_missing" in err_str:
                outcome_holder[0] = "Login failed: required group not assigned. Contact your administrator."
            else:
                outcome_holder[0] = f"Login failed: {err_str}"
            outcome_event.set()
            server_thread.join(5)
            raise LoginStepError(outcome_holder[0]) from exc

    outcome_event.set()
    server_thread.join(5)
    log.info("login_success", issuer_url=issuer_url, client_id=client_id)
    return tokens
