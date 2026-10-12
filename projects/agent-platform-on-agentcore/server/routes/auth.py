"""
User authentication routes: Cognito password login and OIDC (Entra ID) session.

Separate from the MCP gateway auth in routes/mcp.py.

- `GET /config` tells the login screen which providers this deployment has: the
  Cognito password form when the pool and app client are configured, and one
  button per OIDC provider (core/config.OIDC_PROVIDERS). Public, no secrets —
  the OIDC flow is PKCE in the browser, so there is no client secret anywhere.
- `POST /login`, `POST /refresh` are the Cognito password flow
  (USER_PASSWORD_AUTH / REFRESH_TOKEN_AUTH). The id_token carries
  cognito:groups for the UI's role; the access token authorises API calls.
- `POST /session` is called once after an OIDC login with the id_token as
  bearer. core/auth.py has already verified it and resolved the role; this
  records the person (USERS_TABLE, so Insights can name them) and returns the
  profile the UI renders from.

Cognito configuration: COGNITO_USER_POOL_CLIENT_ID, COGNITO_REGION, and
COGNITO_CLIENT_SECRET (optional; when set, SECRET_HASH is computed).
"""
import base64
import hashlib
import hmac
import logging
import os

import boto3
from fastapi import APIRouter, Depends, Request

from core import config
from core.auth import AuthUser, COGNITO_PROVIDER_ID, cognito_login_enabled, current_user
from models.auth import AuthResponse, LoginRequest, RefreshRequest, SessionProfile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _get_cognito_config():
    """Read Cognito config from environment. Returns (client_id, region, secret)."""
    client_id = config.COGNITO_USER_POOL_CLIENT_ID or None
    region = config.COGNITO_REGION
    client_secret = os.getenv("COGNITO_CLIENT_SECRET")
    return client_id, region, client_secret


def _user_repo():
    """The OIDC users table, or None when USERS_TABLE is unset. Imported lazily:
    core.dependencies builds every DynamoDB repository at import, which the
    route tests must not pay for."""
    from core.dependencies import user_repository

    return user_repository


@router.get("/config")
def auth_config(request: Request) -> dict:
    """Login options for this deployment, in the order the screen shows them.

    `redirect_uri` is picked per caller origin so production and a local dev
    server (http://localhost:3000) can share one Entra app registration. The
    web passes its origin as `?origin=`: this is a same-origin GET through the
    Next proxy in every deployment, and browsers attach an Origin header only to
    cross-origin or non-GET requests, so reading the header alone always fell
    back to the first registered callback and the shared registration never
    worked through the proxy. The header is still honoured for callers that
    send one (a cross-origin dev setup, curl).
    """
    providers = []
    if cognito_login_enabled():
        providers.append({
            "id": COGNITO_PROVIDER_ID,
            "kind": "password",
            "label": "아이디 · 비밀번호",
        })
    origin = request.query_params.get("origin") or request.headers.get("origin") or ""
    for p in config.OIDC_PROVIDERS:
        providers.append({
            "id": p.id,
            "kind": "oidc",
            "label": p.label,
            "issuer_url": p.issuer_url,
            "client_id": p.client_id,
            "redirect_uri": p.redirect_uri_for(origin),
            "scopes": p.scopes,
        })
    return {"providers": providers}


@router.post("/session", response_model=SessionProfile)
def session(user: AuthUser = Depends(current_user)) -> SessionProfile:
    """Profile of the verified caller; records OIDC sign-ins for the directory.

    Cognito callers get the same shape back but are not recorded — the pool is
    their directory. A failed write is logged, never surfaced: a directory
    hiccup must not block a login.
    """
    if user.provider and user.provider not in (COGNITO_PROVIDER_ID, "local"):
        repo = _user_repo()
        if repo is not None:
            try:
                repo.upsert_on_login(
                    sub=user.sub,
                    provider=user.provider,
                    email=user.email,
                    name=user.name,
                    groups=user.groups,
                    role="admin" if user.is_admin else "user",
                )
            except Exception as exc:  # noqa: BLE001 - provisioning must not block login
                logger.error("user provisioning failed for %s: %s", user.sub, exc)

    return SessionProfile(
        sub=user.sub,
        username=user.username,
        email=user.email,
        name=user.name,
        role="admin" if user.is_admin else "user",
        groups=user.groups,
        teams=user.teams,
        provider=user.provider,
    )


def _secret_hash(username: str, client_id: str, client_secret: str) -> str:
    """Compute the Cognito SECRET_HASH for a client configured with a secret."""
    message = username + client_id
    dig = hmac.new(
        client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(dig).decode()


def _map_cognito_error(exc: Exception) -> str:
    """Translate a boto3/Cognito exception into a user-facing message."""
    error_type = type(exc).__name__
    error_msg = str(exc)
    if "NotAuthorizedException" in error_type or "NotAuthorizedException" in error_msg:
        return "아이디 또는 비밀번호가 올바르지 않습니다."
    if "UserNotFoundException" in error_type or "UserNotFoundException" in error_msg:
        return "존재하지 않는 사용자입니다."
    if "UserNotConfirmedException" in error_type or "UserNotConfirmedException" in error_msg:
        return "확인되지 않은 계정입니다. 먼저 계정을 확인해주세요."
    logger.error(f"Cognito authentication error: {error_msg}")
    return f"인증에 실패했습니다: {error_msg}"


@router.post("/login", response_model=AuthResponse)
async def login(request: LoginRequest):
    """Authenticate an end-user with Cognito USER_PASSWORD_AUTH."""
    client_id, region, client_secret = _get_cognito_config()

    if not client_id:
        return AuthResponse(
            success=False,
            error="서버에 COGNITO_USER_POOL_CLIENT_ID 환경변수가 설정되어 있지 않습니다.",
        )

    try:
        client = boto3.client("cognito-idp", region_name=region)

        auth_params = {
            "USERNAME": request.username,
            "PASSWORD": request.password,
        }
        if client_secret:
            auth_params["SECRET_HASH"] = _secret_hash(
                request.username, client_id, client_secret
            )

        response = client.initiate_auth(
            ClientId=client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters=auth_params,
        )

        result = response["AuthenticationResult"]
        logger.info(f"User {request.username} logged in successfully")
        return AuthResponse(
            success=True,
            access_token=result.get("AccessToken"),
            id_token=result.get("IdToken"),
            refresh_token=result.get("RefreshToken"),
            expires_in=result.get("ExpiresIn"),
        )
    except Exception as exc:  # noqa: BLE001 - map to friendly message
        return AuthResponse(success=False, error=_map_cognito_error(exc))


@router.post("/refresh", response_model=AuthResponse)
async def refresh(request: RefreshRequest):
    """Refresh tokens with Cognito REFRESH_TOKEN_AUTH.

    Cognito does not return a new refresh_token here; the client keeps the
    existing one.
    """
    client_id, region, client_secret = _get_cognito_config()

    if not client_id:
        return AuthResponse(
            success=False,
            error="서버에 COGNITO_USER_POOL_CLIENT_ID 환경변수가 설정되어 있지 않습니다.",
        )

    try:
        client = boto3.client("cognito-idp", region_name=region)

        auth_params = {"REFRESH_TOKEN": request.refresh_token}
        # For a client with a secret, REFRESH_TOKEN_AUTH still needs SECRET_HASH,
        # computed from the app client id (username part is unused for refresh).
        if client_secret:
            message = client_id
            dig = hmac.new(
                client_secret.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).digest()
            auth_params["SECRET_HASH"] = base64.b64encode(dig).decode()

        response = client.initiate_auth(
            ClientId=client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters=auth_params,
        )

        result = response["AuthenticationResult"]
        return AuthResponse(
            success=True,
            access_token=result.get("AccessToken"),
            id_token=result.get("IdToken"),
            expires_in=result.get("ExpiresIn"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Token refresh failed: {exc}")
        return AuthResponse(success=False, error="세션이 만료되었습니다. 다시 로그인해주세요.")
