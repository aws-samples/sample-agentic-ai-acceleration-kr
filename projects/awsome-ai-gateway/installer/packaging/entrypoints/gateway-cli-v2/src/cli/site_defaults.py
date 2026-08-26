# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Baked-in corporate site defaults.

The deployment environment uses a fixed set of endpoints, OIDC identifiers and a
corporate TLS CA. Rather than shipping those on a per-user onboarding card, we
bake them into the CLI build so an end user can run::

    gateway-cli setup --model claude-sonnet-4-6

with no onboarding card at all — ``--model`` is the only value they ever edit.

Where the values come from
--------------------------
Every value here is **blank by default**. Nothing environment-specific is baked
into a bare build, so a plain checkout applies *nothing* except the model (see
``cli/models.py``). Real values are **injected at build time** into an optional
generated module ``cli/_site_config.py`` (written by ``build.ps1`` from its
``-GatewayUrl`` / ``-AdminApiUrl`` / ``-OidcIssuerUrl`` / ``-OidcClientId`` /
``-CaBundle`` params, the matching ``GATEWAY_CLI_DEFAULT_*`` env vars, or
``packaging/site-config.json``). That generated module is absent from source
control, so no endpoints or secrets are committed; a build without it leaves
every value blank and ``setup`` asks for the required flags (gateway/admin/OIDC)
while the optional TLS/proxy keys are simply not written.

Runtime override
----------------
Every baked value can still be overridden at runtime (a ``GATEWAY_CLI_*`` env
var, then an explicit flag), so the same binary works against staging/dev
endpoints during testing. Precedence for a given field is: baked default <
``GATEWAY_CLI_*`` env var < explicit flag.

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
from pathlib import Path

from cli.manifest import ResolvedConfig

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


# --- Site defaults (all blank unless injected at build time) ----------------
# Every value below is empty in a bare build: nothing environment-specific is
# baked, so a plain checkout/build applies *nothing* by default (the only default
# the tool still applies is the model — see cli/models.py). A real value reaches
# a field only when the build injects it (build.ps1 -Param / GATEWAY_CLI_DEFAULT_*
# / site-config.json → cli/_site_config.py) or the end user supplies it at runtime
# (GATEWAY_CLI_* env / explicit flag). When these stay blank, `setup` asks for the
# required flags (gateway/admin/OIDC) and the optional TLS/proxy keys are simply
# not written.
DEFAULT_GATEWAY_URL = _baked("GATEWAY_URL", "")
DEFAULT_ADMIN_API_URL = _baked("ADMIN_API_URL", "")

DEFAULT_OIDC_ISSUER_URL = _baked("OIDC_ISSUER_URL", "")
DEFAULT_OIDC_CLIENT_ID = _baked("OIDC_CLIENT_ID", "")

# Corporate TLS CA (PEM) location on the target machine. Blank unless a CA is
# baked at build time (--ca-bundle / CA_BUNDLE) or supplied at runtime
# (GATEWAY_CLI_CA_BUNDLE). With no CA configured the CA-bundle env vars
# (NODE_EXTRA_CA_CERTS etc.) are never written, and the process relies on the OS
# trust store (truststore). Production builds on the isolated network should bake
# an explicit PEM path (`--ca-bundle C:\corp-ca.pem` / `/etc/ssl/certs/<corp>.pem`).
DEFAULT_CA_BUNDLE = _baked("CA_BUNDLE", "")

# --- Config value resolution ------------------------------------------------

#: camelCase config key ⇄ the catalog field that resolves it. The field's
#: ``env_override`` / ``baked_from`` match this module's ``DEFAULT_*`` baked
#: constants and the ``GATEWAY_CLI_*`` env names, so routing through ``resolve``
#: gives the baked < env < flag precedence from a single path (finding:
#: fragmentation — the old ``_ENV_OVERRIDES`` / ``default_config_values`` walk
#: was removed in favour of ``resolve``).
_CONFIG_FIELD_BY_JSON: dict[str, str] = {
    "gatewayUrl": "ANTHROPIC_BASE_URL",
    "adminApiUrl": "ADMIN_API_URL",
    "oidcIssuerUrl": "OIDC_ISSUER_URL",
    "oidcClientId": "OIDC_CLIENT_ID",
}


def resolve_config(overrides: dict[str, str] | None = None) -> ResolvedConfig:
    """Resolve the effective gateway/OIDC config for one command invocation.

    Precedence: baked corporate defaults < ``GATEWAY_CLI_*`` env overrides <
    explicit flag ``overrides``. Used by ``login`` / ``setup`` / ``verify`` so the
    golden path works with no per-user input — the baked values fill every gap.

    The four URL/identity values are resolved through the unified
    :func:`cli.resolve.resolve` (imported lazily to avoid an import cycle), which
    walks flag → ``GATEWAY_CLI_*`` env → baked default and validates each — the
    same precedence the bespoke walk used, now single-sourced from the catalog.
    Any other override key (``email``, which has no catalog field) is passed
    through verbatim, preserving the previous ``values.update(overrides)`` shape.
    """
    from cli.manifest import by_key
    from cli.resolve import Sources, resolve

    overrides = overrides or {}
    values: dict[str, str] = {}
    for json_key, field_key in _CONFIG_FIELD_BY_JSON.items():
        flags = {field_key: overrides.get(json_key, "")}
        resolved = resolve(by_key(field_key), Sources(flags=flags, env=os.environ))
        if resolved.value:
            values[json_key] = resolved.value

    # Non-field overrides (e.g. email) pass straight through, blanks dropped.
    for key, value in overrides.items():
        if key not in _CONFIG_FIELD_BY_JSON and value and str(value).strip():
            values[key] = str(value).strip()

    return ResolvedConfig.from_values(values)


# --- CA bundle --------------------------------------------------------------

def configured_ca_bundle() -> str | None:
    """The CA/PEM path to write into settings for the *target* machine.

    Returns the env override or the baked path **regardless of whether the file
    exists here** — settings are consumed on the target machine where the PEM is
    present, which may not be this build/dev host. Returns None only if no path
    is configured at all.

    Resolved through the unified :func:`cli.resolve.resolve` on the representative
    CA field (all four CA fields share ``env_override=GATEWAY_CLI_CA_BUNDLE`` and
    ``baked_from=CA_BUNDLE``), giving the same ``GATEWAY_CLI_CA_BUNDLE`` env >
    baked ``DEFAULT_CA_BUNDLE`` precedence, now single-sourced from the catalog.
    """
    from cli.manifest import by_key
    from cli.resolve import Sources, resolve

    resolved = resolve(by_key("NODE_EXTRA_CA_CERTS"), Sources(env=os.environ))
    return resolved.value or None


def resolved_ca_bundle() -> str | None:
    """The CA/PEM path to apply to *this* process, only if it exists locally."""
    candidate = configured_ca_bundle()
    if candidate and Path(candidate).is_file():
        return candidate
    return None


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
