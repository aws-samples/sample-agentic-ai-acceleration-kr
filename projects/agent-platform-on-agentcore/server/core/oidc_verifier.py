"""Generic OIDC id_token verification (Entra ID, Keycloak, Okta, ...). PyJWT only.

The issuer's ``.well-known/openid-configuration`` names the JWKS; PyJWKClient
caches the keys (lifespan = OIDC_JWKS_CACHE_TTL_SECONDS) and matches ``kid`` so
key rotation is safe. Only RS256/384/512 are accepted — ``alg=none`` and HS*
are refused before any key lookup.

Several providers can be configured at once: ``verify_any`` routes a token by
its (unverified) ``iss`` to the matching provider and verifies with that
provider's audience. The Cognito access-token path is not here — core/auth.py
handles it with the pool's own JWKS, because Cognito access tokens carry no
``aud`` and the role comes from ``cognito:groups``.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from threading import Lock
from typing import Dict, List, Optional, Tuple

import jwt

logger = logging.getLogger(__name__)

_ALLOWED_ALGS = ("RS256", "RS384", "RS512")


class OIDCVerifyError(Exception):
    """The token failed verification. Maps to 401."""


class OIDCConfigError(Exception):
    """Discovery or JWKS retrieval failed. Maps to 503."""


class OIDCVerifier:
    def __init__(
        self,
        issuer_url: str,
        audience: str,
        jwks_cache_ttl_seconds: int = 3600,
        http_timeout_seconds: float = 5.0,
        discovery_url_override: Optional[str] = None,
    ) -> None:
        if not issuer_url:
            raise ValueError("issuer_url required")
        # `iss` is always compared against issuer_url exactly; only the JWKS
        # fetch can be redirected (private discovery mirrors, tests).
        self._issuer_url = issuer_url.rstrip("/")
        self._discovery_base = (discovery_url_override or issuer_url).rstrip("/")
        # An empty audience skips the `aud` check. Entra id_tokens always carry
        # the client id, so deployments fill it.
        self._audience = audience or None
        self._jwks_ttl = jwks_cache_ttl_seconds
        self._http_timeout = http_timeout_seconds

        self._jwks_uri: Optional[str] = None
        self._jwks_client: Optional[jwt.PyJWKClient] = None
        self._lock = Lock()

    def _get_json(self, url: str, timeout: float) -> dict:
        """JSON GET via stdlib. The seam tests monkeypatch."""
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    def _discover_jwks_uri(self) -> str:
        url = f"{self._discovery_base}/.well-known/openid-configuration"
        try:
            data = self._get_json(url, self._http_timeout)
        except Exception as exc:  # noqa: BLE001 - network/parse failures are all config errors
            raise OIDCConfigError(f"OIDC discovery failed: {exc}") from exc

        jwks_uri = data.get("jwks_uri")
        if not jwks_uri:
            raise OIDCConfigError("OIDC discovery response missing jwks_uri")

        disc_issuer = (data.get("issuer") or "").rstrip("/")
        if disc_issuer != self._issuer_url:
            raise OIDCConfigError(
                f"OIDC issuer mismatch: configured={self._issuer_url} discovered={disc_issuer}"
            )
        return jwks_uri

    def _ensure_client(self) -> jwt.PyJWKClient:
        if self._jwks_client is not None:
            return self._jwks_client
        with self._lock:
            if self._jwks_client is not None:
                return self._jwks_client
            if self._jwks_uri is None:
                self._jwks_uri = self._discover_jwks_uri()
            self._jwks_client = jwt.PyJWKClient(self._jwks_uri, lifespan=self._jwks_ttl)
            logger.info("oidc_verifier.jwks_client_ready issuer=%s", self._issuer_url)
            return self._jwks_client

    def verify(self, token: str) -> dict:
        """Claims on success. Token problems raise OIDCVerifyError, infrastructure
        problems OIDCConfigError."""
        if not token or token.count(".") != 2:
            raise OIDCVerifyError("malformed token")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OIDCVerifyError(f"invalid header: {exc}") from exc

        if header.get("alg") not in _ALLOWED_ALGS:
            raise OIDCVerifyError(f"algorithm not allowed: {header.get('alg')}")

        client = self._ensure_client()

        try:
            signing_key = client.get_signing_key_from_jwt(token)
        except jwt.PyJWTError as exc:
            # Includes an unknown kid — treated as the token's fault (401).
            raise OIDCVerifyError(f"no signing key: {exc}") from exc

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(_ALLOWED_ALGS),
                issuer=self._issuer_url,
                audience=self._audience,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": self._audience is not None,
                    "require": ["exp"],
                },
            )
        except jwt.PyJWTError as exc:
            raise OIDCVerifyError(str(exc)) from exc

        if not claims.get("sub"):
            raise OIDCVerifyError("missing 'sub' claim")
        return claims


# ── per-issuer verifier registry ─────────────────────────────────────────────
_registry: Dict[str, OIDCVerifier] = {}
_registry_lock = Lock()


def _verifier_for(provider) -> OIDCVerifier:
    """One cached verifier per provider issuer (core.config.OIDCProvider)."""
    key = provider.issuer
    existing = _registry.get(key)
    if existing is not None:
        return existing
    with _registry_lock:
        existing = _registry.get(key)
        if existing is not None:
            return existing
        from core import config

        verifier = OIDCVerifier(
            issuer_url=provider.issuer_url,
            audience=provider.audience,
            jwks_cache_ttl_seconds=config.OIDC_JWKS_CACHE_TTL_SECONDS,
            discovery_url_override=config.OIDC_DISCOVERY_URL_OVERRIDE or None,
        )
        _registry[key] = verifier
        return verifier


def unverified_issuer(token: str) -> str:
    """The `iss` claim read without verifying the signature — only ever used to
    pick which verifier to run. Nothing is trusted until that verifier has
    checked signature, iss, aud and exp."""
    if not token or token.count(".") != 2:
        raise OIDCVerifyError("malformed token")
    try:
        payload = jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        raise OIDCVerifyError(f"malformed token: {exc}") from exc
    return (payload.get("iss") or "").rstrip("/")


def verify_any(token: str, providers: List) -> Tuple[dict, object]:
    """Pick the provider by `iss` and verify. Returns (claims, provider).

    An `iss` matching no provider is OIDCVerifyError (401), as are signature,
    expiry and audience failures. Discovery/JWKS outages propagate as
    OIDCConfigError (503).
    """
    iss = unverified_issuer(token)
    provider = next((p for p in providers if p.issuer == iss), None)
    if provider is None:
        raise OIDCVerifyError(f"unknown issuer: {iss or '(none)'}")
    claims = _verifier_for(provider).verify(token)
    return claims, provider


def reset_verifier_cache() -> None:
    """Drop every cached verifier. Tests and configuration reloads."""
    global _registry
    with _registry_lock:
        _registry = {}
