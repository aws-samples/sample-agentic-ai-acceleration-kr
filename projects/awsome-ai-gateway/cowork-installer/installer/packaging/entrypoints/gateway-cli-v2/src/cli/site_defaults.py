# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Baked-in corporate site defaults.

The corporate environment uses a fixed set of endpoints, OIDC identifiers and a
corporate TLS CA. We bake them into the CLI build so an end user can run::

    gateway-cli setup --model cowork-opus

with no per-user configuration at all — ``--model`` is the only value they ever edit.

Where the values come from
--------------------------
The public, non-secret values (the two domains and the CA/PEM path) are literal
defaults in this module. The two OIDC values (``oidcIssuerUrl`` /
``oidcClientId``) are environment-specific and are **injected at build time**
into an optional generated module ``cli/_site_config.py`` (written by
``build.ps1`` from ``-OidcIssuerUrl`` / ``-OidcClientId`` params or the matching
env vars). That generated module is absent from source control, so no secrets
are committed; a dev build without it simply leaves the OIDC values blank and
``setup`` will ask for ``--oidc-*`` flags.

Runtime override
----------------
Every baked value can still be overridden at runtime via a ``GATEWAY_CLI_*`` env
var, so the same binary works against staging/dev endpoints during testing.
Precedence for a given field is: baked default < env override.

CA bundle / TLS
---------------
On the isolated network the gateway/OIDC endpoints are fronted by a corporate CA
that PyInstaller's bundled ``certifi`` does not trust. We bake the PEM path and
(a) apply it to gateway-cli's own process so ``login`` / ``verify`` HTTPS trusts
it, and (b) write it into Claude Code's settings as ``NODE_EXTRA_CA_CERTS`` (plus
``REQUESTS_CA_BUNDLE`` / ``AWS_CA_BUNDLE`` / ``SSL_CERT_FILE`` for the helpers).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Optional build-time-injected overrides --------------------------------
# build.ps1 writes cli/_site_config.py with the environment-specific values
# (chiefly the two OIDC identifiers). Absent in a plain dev checkout.
try:  # pragma: no cover - presence depends on the build
    from cli import _site_config as _cfg  # type: ignore
except ImportError:  # pragma: no cover
    _cfg = None  # type: ignore


def _baked(name: str, fallback: str) -> str:
    """Return a build-injected value if present, else the literal fallback."""
    if _cfg is not None:
        value = getattr(_cfg, name, None)
        if value:
            return str(value)
    return fallback


# --- Public, non-secret corporate defaults ---------------------------------
DEFAULT_GATEWAY_URL = _baked("GATEWAY_URL", "https://gateway.example.com")
DEFAULT_ADMIN_API_URL = _baked("ADMIN_API_URL", "https://api.gateway.example.com")

# Environment-specific — blank unless injected at build time.
DEFAULT_OIDC_ISSUER_URL = _baked("OIDC_ISSUER_URL", "")
DEFAULT_OIDC_CLIENT_ID = _baked("OIDC_CLIENT_ID", "")

# Corporate TLS CA (PEM) location on the target machine. The literal fallback is
# platform-specific: a Windows path only makes sense on Windows, and writing it
# into a Linux build's NODE_EXTRA_CA_CERTS (etc.) would point Node/boto at a path
# that cannot exist on Linux. When no CA is baked at build time (--ca-bundle /
# CA_BUNDLE), Linux/macOS fall back to no path and rely on the OS trust store
# (truststore) instead; production Linux builds should bake an explicit Linux PEM
# path via `--ca-bundle /etc/ssl/certs/<corp-ca>.pem`.
_DEFAULT_CA_BUNDLE_FALLBACK = r"C:\corp-proxy-ca.pem" if sys.platform == "win32" else ""
DEFAULT_CA_BUNDLE = _baked("CA_BUNDLE", _DEFAULT_CA_BUNDLE_FALLBACK)

# --- Cowork (Claude Desktop) managed-config defaults ------------------------
# These feed the managed-config writer (cowork_config, Interval 2.1) that points
# the Desktop app at the gateway. All are environment-specific, so they are blank
# unless injected at build time (build.ps1) — a dev checkout leaves them empty and
# `setup` reports them missing, exactly like the OIDC values above.
#
# NOTE: coworkGatewayHttpsUrl is NOT the same as DEFAULT_GATEWAY_URL. The latter
# is the admin/proxy URL the CLI/helper call; this is the app's inference base
# (`inferenceGatewayBaseUrl`) and MUST be a publicly-trusted HTTPS origin
# (CloudFront) — the app rejects HTTP (originPinned). See
# docs/FILE_AND_ENV_OPERATIONS.md §"inference/gateway config keys".
DEFAULT_COWORK_GATEWAY_URL = _baked("COWORK_GATEWAY_URL", "")

# Org attribution tag stamped on Cowork telemetry (`deploymentOrganizationUuid`).
# Generate once per org (uuidgen) and pin at build time.
DEFAULT_ORG_UUID = _baked("ORG_UUID", "")

# Expected SHA-256 fingerprint (uppercase hex, no separators) of the corporate CA
# that cowork_ca is allowed to install. Blank = no pin (dev builds); when baked,
# the CA hardening (Interval 1.4) refuses to install any PEM whose fingerprint
# does not match this, so a swapped/attacker PEM cannot become a trusted root.
DEFAULT_CA_SHA256 = _baked("CA_SHA256", "").strip().upper()

# Baked default ``inferenceModels`` roster, comma-separated (first = default).
# Blank = fall back to cli.models.FALLBACK_MODELS. A setup-time
# --available-models still overrides this. Each alias must exist in the gateway's
# model.model_aliases table, else it fails with model-not-found.
DEFAULT_COWORK_MODELS = _baked("COWORK_MODELS", "")

# Env vars that override the baked defaults at runtime (staging/dev testing).
_ADMIN_API_ENV_VAR = "GATEWAY_CLI_ADMIN_API_URL"
_OIDC_ISSUER_ENV_VAR = "GATEWAY_CLI_OIDC_ISSUER_URL"
_OIDC_CLIENT_ENV_VAR = "GATEWAY_CLI_OIDC_CLIENT_ID"
_CA_ENV_VAR = "GATEWAY_CLI_CA_BUNDLE"
_COWORK_GATEWAY_ENV_VAR = "GATEWAY_CLI_COWORK_GATEWAY_URL"
_ORG_UUID_ENV_VAR = "GATEWAY_CLI_ORG_UUID"
_CA_SHA256_ENV_VAR = "GATEWAY_CLI_CA_SHA256"
_COWORK_MODELS_ENV_VAR = "GATEWAY_CLI_COWORK_MODELS"


# --- Corporate endpoint / OIDC accessors ------------------------------------
# ``login`` / ``verify`` read these values directly from the build-time-baked
# defaults (see ``_site_config.py``). A runtime ``GATEWAY_CLI_*`` env var
# overrides any field for staging/dev testing.
# Nothing is persisted between commands — production builds bake their endpoints,
# so every command resolves the same values without a saved config file. Each
# accessor returns "" when neither an env override nor a baked value is set (e.g.
# an un-injected OIDC identifier in a dev build), so callers can presence-check.

def _resolve_str(env_var: str, baked: str) -> str:
    """Runtime env override, else the baked value, stripped; "" when neither set."""
    override = os.environ.get(env_var, "")
    chosen = override if override and override.strip() else (baked or "")
    return chosen.strip()


def admin_api_url() -> str:
    """The admin API base URL (``adminApiUrl``) for the VK exchange, or "" when unset."""
    return _resolve_str(_ADMIN_API_ENV_VAR, DEFAULT_ADMIN_API_URL)


def oidc_issuer_url() -> str:
    """The OIDC issuer URL (``oidcIssuerUrl``), or "" when unset."""
    return _resolve_str(_OIDC_ISSUER_ENV_VAR, DEFAULT_OIDC_ISSUER_URL)


def oidc_client_id() -> str:
    """The OIDC client id (``oidcClientId``), or "" when unset."""
    return _resolve_str(_OIDC_CLIENT_ENV_VAR, DEFAULT_OIDC_CLIENT_ID)


# --- CA bundle --------------------------------------------------------------

def configured_ca_bundle() -> str | None:
    """The CA/PEM path to write into settings for the *target* machine.

    Returns the env override or the baked path **regardless of whether the file
    exists here** — settings are consumed on the target machine where the PEM is
    present, which may not be this build/dev host. Returns None only if no path
    is configured at all.
    """
    candidate = os.environ.get(_CA_ENV_VAR) or DEFAULT_CA_BUNDLE
    candidate = candidate.strip() if candidate else ""
    return candidate or None


def resolved_ca_bundle() -> str | None:
    """The CA/PEM path to apply to *this* process, only if it exists locally."""
    candidate = configured_ca_bundle()
    if candidate and Path(candidate).is_file():
        return candidate
    return None


def ca_bundle_is_env_override() -> bool:
    """True when the CA PEM path comes from ``GATEWAY_CLI_CA_BUNDLE`` (an override).

    Lets the CA installer distinguish the build-time baked default (trusted by
    construction) from a runtime-supplied PEM path, which is a deliberate
    deviation the installer should not honour silently (see cowork_ca pinning).
    """
    return bool((os.environ.get(_CA_ENV_VAR) or "").strip())


def apply_ca_bundle() -> str | None:
    """Point this process's TLS stack at the corporate CA if the PEM is present.

    Sets the standard CA-bundle env vars (without clobbering any the user already
    set) so gateway-cli's own ``login`` / ``verify`` HTTPS calls — and any boto3
    calls — trust the corporate CA. No-op when the PEM is not on disk (e.g. dev
    machines), so it never breaks local testing. Returns the path applied or None.
    """
    ca = resolved_ca_bundle()
    if not ca:
        return None
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "AWS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        os.environ.setdefault(var, ca)
    return ca


# --- Cowork managed-config accessors ----------------------------------------

def _resolve_baked(env_var: str, baked: str) -> str | None:
    """Runtime env override, else the baked value; None when neither is set.

    Same resolution as :func:`_resolve_str`, but returns None instead of "" so the
    Cowork managed-config accessors can express "unconfigured" distinctly.
    """
    return _resolve_str(env_var, baked) or None


def cowork_gateway_url() -> str | None:
    """The Cowork inference base URL (``inferenceGatewayBaseUrl``), or None.

    Publicly-trusted HTTPS origin (CloudFront). Env override
    ``GATEWAY_CLI_COWORK_GATEWAY_URL`` wins over the baked value for dev/staging.
    """
    return _resolve_baked(_COWORK_GATEWAY_ENV_VAR, DEFAULT_COWORK_GATEWAY_URL)


def cowork_org_uuid() -> str | None:
    """The org attribution UUID (``deploymentOrganizationUuid``), or None."""
    return _resolve_baked(_ORG_UUID_ENV_VAR, DEFAULT_ORG_UUID)


def cowork_default_models() -> list[str]:
    """The baked default ``inferenceModels`` roster (first entry = default).

    Resolution order: env override ``GATEWAY_CLI_COWORK_MODELS`` > build-time
    baked ``COWORK_MODELS`` > ``cli.models.FALLBACK_MODELS``. Each is a
    comma-separated list; every alias must be registered in the gateway's
    ``model.model_aliases``. A setup-time ``--available-models`` still overrides
    this at the CLI layer.
    """
    # Inline to avoid a circular import (models may import site_defaults).
    from cli.models import FALLBACK_MODELS, parse_available_models

    raw = _resolve_baked(_COWORK_MODELS_ENV_VAR, DEFAULT_COWORK_MODELS)
    parsed = parse_available_models(raw)
    return parsed or list(FALLBACK_MODELS)


def expected_ca_sha256() -> str | None:
    """The pinned CA SHA-256 fingerprint cowork_ca must match, or None.

    None means "no pin baked" (dev build) — the CA install proceeds unpinned.
    Env override ``GATEWAY_CLI_CA_SHA256`` wins for testing against a different CA.
    """
    value = _resolve_baked(_CA_SHA256_ENV_VAR, DEFAULT_CA_SHA256)
    return value.upper() if value else None
