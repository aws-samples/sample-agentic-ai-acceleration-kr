"""
Bearer-token verification and role checks, for Cognito and OIDC providers.

The frontend's role gate is a convenience, not a control: it hides admin
buttons but cannot stop a direct call to /api/*. These dependencies are what
actually enforce the boundary.

Two kinds of token arrive, told apart by `iss`:

- **Cognito access token** from the password login (/api/auth/login). Cognito
  puts cognito:groups on both tokens, but the access token is the API
  authorisation token, carries no `aud`, and the admin group is `admin`.
- **OIDC id_token** from a browser PKCE login against a configured provider
  (Microsoft Entra ID). The id_token is the one that carries groups and the
  client-id audience; the provider's own groups-claim / admin rules decide the
  role (core/config.OIDCProvider).

Either path yields the same AuthUser, so nothing downstream knows which IdP
signed someone in — except `provider`, kept so per-user records can say so.
"""
import logging
from typing import List, Optional

import jwt
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

import core.oidc_verifier as ov
from core import config

logger = logging.getLogger(__name__)

ADMIN_GROUP = "admin"
# A Cognito group named `team:<name>` makes its members part of team <name>.
# Teams decide which registry records a person sees, which harness execution
# role their harnesses run under, and which models they may pick.
TEAM_GROUP_PREFIX = "team:"
COGNITO_PROVIDER_ID = "cognito"


class TokenInvalid(Exception):
    """The bearer token failed verification."""


class AuthUser(BaseModel):
    # The IdP `sub`. Per-user resources are keyed by this rather than by
    # username: a username can be reassigned after an account is deleted, which
    # would hand the new account the old one's knowledge bases.
    sub: str = ""
    username: str
    email: str = ""
    name: str = ""
    groups: List[str] = []
    # "admin" | "user". Empty means "derive from groups" (the Cognito rule),
    # which keeps callers that build an AuthUser from groups alone working.
    role: str = ""
    # Which login produced this user: "cognito" or an OIDC provider id.
    provider: str = ""

    @property
    def is_admin(self) -> bool:
        if self.role:
            return self.role == "admin"
        return ADMIN_GROUP in self.groups

    @property
    def teams(self) -> List[str]:
        """Team names from `team:` groups, in group order, without duplicates."""
        seen: List[str] = []
        for group in self.groups:
            if isinstance(group, str) and group.startswith(TEAM_GROUP_PREFIX):
                name = group[len(TEAM_GROUP_PREFIX):]
                if name and name not in seen:
                    seen.append(name)
        return seen


# PyJWKClient caches the fetched JWKS, so this is built once per pool.
_jwk_client: Optional[jwt.PyJWKClient] = None


def reset_verifier_cache() -> None:
    """Drop the cached JWKS clients. Used by tests."""
    global _jwk_client
    _jwk_client = None
    ov.reset_verifier_cache()


def _cognito_issuer() -> str:
    return f"https://cognito-idp.{config.COGNITO_REGION}.amazonaws.com/{config.COGNITO_USER_POOL_ID}"


def _jwks() -> jwt.PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = jwt.PyJWKClient(f"{_cognito_issuer()}/.well-known/jwks.json")
    return _jwk_client


def _decode_token(token: str) -> dict:
    """Verify a Cognito token's signature, expiry and issuer. Raises TokenInvalid."""
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_cognito_issuer(),
            # Cognito access tokens carry no `aud`, so audience is not checked.
            options={"verify_aud": False, "require": ["exp"]},
        )
    except Exception as exc:
        raise TokenInvalid(str(exc)) from exc


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")
    return token.strip()


def cognito_login_enabled() -> bool:
    """The password form needs the app client; verification needs the pool."""
    return bool(config.COGNITO_USER_POOL_ID and config.COGNITO_USER_POOL_CLIENT_ID)


def _user_from_cognito(token: str) -> AuthUser:
    try:
        claims = _decode_token(token)
    except TokenInvalid as exc:
        logger.info("Rejected Cognito bearer token: %s", exc)
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    if claims.get("token_use") != "access":
        raise HTTPException(
            status_code=401,
            detail="액세스 토큰이 필요합니다 (id 토큰은 사용할 수 없습니다).",
        )

    groups = claims.get("cognito:groups")
    groups = list(groups) if isinstance(groups, list) else []
    return AuthUser(
        sub=claims.get("sub") or "",
        username=claims.get("username") or claims.get("sub") or "",
        groups=groups,
        role="admin" if ADMIN_GROUP in groups else "user",
        provider=COGNITO_PROVIDER_ID,
    )


def _user_from_oidc(token: str) -> AuthUser:
    try:
        claims, provider = ov.verify_any(token, config.OIDC_PROVIDERS)
    except ov.OIDCVerifyError as exc:
        logger.info("Rejected OIDC bearer token: %s", exc)
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")
    except ov.OIDCConfigError as exc:
        logger.error("OIDC verification unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="인증 서비스를 일시적으로 사용할 수 없습니다.")

    groups = provider.groups_of(claims)
    if not provider.allows(groups):
        raise HTTPException(status_code=403, detail="이 애플리케이션에 대한 접근 권한이 없습니다.")

    email = claims.get(config.OIDC_EMAIL_CLAIM) or claims.get("preferred_username") or ""
    if "@" not in str(email):
        email = ""
    name = claims.get(config.OIDC_NAME_CLAIM) or ""
    return AuthUser(
        sub=claims.get("sub") or "",
        username=claims.get("preferred_username") or email or claims.get("sub") or "",
        email=str(email),
        name=str(name),
        groups=groups,
        role=provider.role_for(str(email), groups),
        provider=provider.id,
    )


def current_user(request: Request) -> AuthUser:
    """Any authenticated caller. Read paths use this without checking the role."""
    if not config.AUTH_ENFORCED:
        # Local development without any identity provider.
        return AuthUser(sub="local", username="local", groups=[ADMIN_GROUP], role="admin", provider="local")

    if not config.COGNITO_USER_POOL_ID and not config.OIDC_PROVIDERS:
        # Fail closed: an unconfigured IdP must not silently disable auth.
        raise HTTPException(
            status_code=503,
            detail=(
                "COGNITO_USER_POOL_ID 도 OIDC provider 도 설정되지 않아 요청을 "
                "인증할 수 없습니다."
            ),
        )

    token = _bearer_token(request)
    try:
        issuer = ov.unverified_issuer(token)
    except ov.OIDCVerifyError:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    # Routing only: the chosen path re-verifies `iss` against the signature.
    if config.COGNITO_USER_POOL_ID and issuer == _cognito_issuer():
        return _user_from_cognito(token)
    if config.OIDC_PROVIDERS:
        return _user_from_oidc(token)
    # Cognito-only deployment handed a token from somewhere else.
    logger.info("Rejected bearer token from unknown issuer %s", issuer or "(none)")
    raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")


def require_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    """Write paths. 403 rather than 401: the caller is known, just unprivileged."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return user
