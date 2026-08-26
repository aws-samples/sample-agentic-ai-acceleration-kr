# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Cowork (Claude Desktop) end-to-end health check.

:func:`run_cowork_verify` verifies the full stack from the end-user's machine:

  1. Managed config present, in gateway (3P) mode, and where it lives
  2. HKLM precedence trap (Windows)
  3. Credential channel — the inferenceCredentialHelper path exists on disk
  4. Inference base URL reachable over HTTPS (publicly-trusted CloudFront)
  5. Egress proxy — the mandatory downloads.claude.ai bundle fetch
  6. Corporate CA trust + fingerprint pin (the OS trust store Chromium reads)
  7. Corporate forward-proxy env (HTTP_PROXY/HTTPS_PROXY value + NO_PROXY)
  8. OTLP telemetry keys carried in the managed config (optional)
  9. Helper token/VK health (helper-script kind only)

Reachability checks use a short timeout (5 s) so the command fails fast when a
host is unreachable; the config/token/CA checks are local and never make network
calls.

Each check reports one of two states — there is no middle "warning" tier: a
check is FAIL only when the setup is actually broken (no managed config, a
missing credential-helper path, an unreachable endpoint, a CA fingerprint
mismatch, a foreign HKLM policy). Everything else — token/VK freshness, an
absent corporate CA when none is configured, proxy values, the credential kind,
OTLP — is reported OK with the detail still shown, so the status stays simple.

Exit codes:
  OK   — all checks pass (exit 0)
  FAIL — one or more checks failed (exit 1)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import requests
import structlog

from cli.paths import oidc_tokens_path, prog_name, vk_cache_path
from cli.site_defaults import (
    admin_api_url,
    oidc_client_id,
    oidc_issuer_url,
)

log = structlog.get_logger(component="cli-v2")

REQUEST_TIMEOUT = 5.0  # seconds


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class CheckStatus(Enum):
    OK = "ok"
    FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


@dataclass
class VerifyOutcome:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> CheckStatus:
        if any(c.status == CheckStatus.FAIL for c in self.checks):
            return CheckStatus.FAIL
        return CheckStatus.OK

    def add(self, name: str, status: CheckStatus, detail: str) -> None:
        self.checks.append(CheckResult(name=name, status=status, detail=detail))


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_oidc_tokens(issuer_url: str, client_id: str, outcome: VerifyOutcome) -> None:
    """Verify cached OIDC tokens exist and are not expired."""
    path = oidc_tokens_path()

    if not path.exists():
        outcome.add(
            "oidc-tokens",
            CheckStatus.FAIL,
            f"no token cache at {path} — run `{prog_name()} login`",
        )
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        outcome.add("oidc-tokens", CheckStatus.FAIL, f"cannot read token cache: {e} [{path}]")
        return

    cached_issuer = data.get("issuer_url", "")
    cached_client = data.get("client_id", "")
    if cached_issuer != issuer_url or cached_client != client_id:
        outcome.add(
            "oidc-tokens",
            CheckStatus.OK,
            f"token cache is for a different IDP ({cached_issuer}) — re-login may be needed [{path}]",
        )
        return

    expires_at = float(data.get("expires_at", 0))
    remaining = expires_at - time.time()
    if remaining <= 0:
        outcome.add(
            "oidc-tokens",
            CheckStatus.OK,
            f"access token is expired — re-login may be needed (api-key-helper uses refresh token) [{path}]",
        )
    elif remaining < 120:
        outcome.add("oidc-tokens", CheckStatus.OK, f"access token expires in {int(remaining)}s [{path}]")
    else:
        outcome.add("oidc-tokens", CheckStatus.OK, f"valid, expires in {int(remaining)}s [{path}]")


def _check_vk_cache(issuer_url: str, admin_api_url: str, outcome: VerifyOutcome) -> None:
    """Verify the Virtual Key cache exists and is not expired."""
    path = vk_cache_path()

    if not path.exists():
        outcome.add(
            "vk-cache",
            CheckStatus.OK,
            f"no VK cache at {path} — will be created on next api-key-helper call",
        )
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        outcome.add("vk-cache", CheckStatus.OK, f"cannot read VK cache: {e} [{path}]")
        return

    cached_issuer = data.get("issuer_url", "")
    cached_api = data.get("admin_api_url", "")
    if cached_issuer and cached_issuer != issuer_url:
        outcome.add(
            "vk-cache",
            CheckStatus.OK,
            f"VK cache is for a different IDP ({cached_issuer}) — re-login needed [{path}]",
        )
        return
    if cached_api and cached_api.rstrip("/") != admin_api_url.rstrip("/"):
        outcome.add(
            "vk-cache",
            CheckStatus.OK,
            f"VK cache is for a different admin API ({cached_api}) — re-login needed [{path}]",
        )
        return

    expires_at = float(data.get("expires_at", 0))
    remaining = expires_at - time.time()
    if remaining <= 0:
        outcome.add(
            "vk-cache",
            CheckStatus.OK,
            f"Virtual Key is expired — will be renewed on next api-key-helper call [{path}]",
        )
    elif remaining < 1800:
        outcome.add("vk-cache", CheckStatus.OK, f"Virtual Key expires soon ({int(remaining)}s) [{path}]")
    else:
        outcome.add("vk-cache", CheckStatus.OK, f"valid, expires in {int(remaining // 60)}m [{path}]")


def _effective_proxy_env() -> dict[str, str]:
    """Return the proxy vars from the OS/process environment.

    Cowork's Chromium stack honours the OS proxy environment, so verify checks
    exactly those variables — it does not consult any Claude Code settings file.
    """
    return {var: os.environ.get(var, "") for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")}


def _check_proxy_settings(outcome: VerifyOutcome) -> None:
    """Validate the corporate proxy config.

    On the isolated network, HTTP_PROXY / HTTPS_PROXY should equal the corporate
    forward proxy (``cli.managed.EXPECTED_PROXY_URL``) and NO_PROXY should NOT
    contain the corporate suffix (``cli.managed.FORBIDDEN_NO_PROXY_TOKEN``) —
    listing that suffix as direct breaks routing. These are reported OK either
    way (a proxy value that does not match is common off the corporate network); the
    detail states the expected value so an operator can compare.
    """
    from cli.managed import EXPECTED_PROXY_URL, FORBIDDEN_NO_PROXY_TOKEN  # noqa: PLC0415

    env = _effective_proxy_env()

    for var in ("HTTP_PROXY", "HTTPS_PROXY"):
        value = (env.get(var) or "").strip()
        if value == EXPECTED_PROXY_URL:
            outcome.add(f"proxy:{var}", CheckStatus.OK, f"{var}={value}")
        else:
            shown = value or "(미설정 / not set)"
            outcome.add(
                f"proxy:{var}",
                CheckStatus.OK,
                f"{var}={shown} (expected {EXPECTED_PROXY_URL} on the corporate network)",
            )

    no_proxy = (env.get("NO_PROXY") or "").strip()
    if FORBIDDEN_NO_PROXY_TOKEN in no_proxy:
        outcome.add(
            "proxy:NO_PROXY",
            CheckStatus.OK,
            f"NO_PROXY={no_proxy} "
            f"(note: '{FORBIDDEN_NO_PROXY_TOKEN}' should not be listed on the corporate network)",
        )
    else:
        shown = no_proxy or "(미설정 / not set)"
        outcome.add("proxy:NO_PROXY", CheckStatus.OK, f"NO_PROXY={shown}")


# ---------------------------------------------------------------------------
# Cowork (Claude Desktop) checks
# ---------------------------------------------------------------------------
# These target the Cowork surfaces: the OS-native managed-config channel
# (registry / configLibrary), the HKLM precedence trap, the inference URL +
# egress-proxy TLS reachability, the corporate-CA trust/fingerprint pin, and
# (helper-script kind) the shared helper token health.

# The mandatory bundle-fetch host Cowork hits through the corporate egress proxy;
# if the corporate CA is not trusted by the OS store, this handshake fails — which
# is exactly the failure this check surfaces (see cli.cowork_ca).
_COWORK_EGRESS_URL = "https://downloads.claude.ai/"


def _check_https_reachable(name: str, url: str, outcome: VerifyOutcome) -> None:
    """Confirm ``url`` is reachable over HTTPS (any HTTP response proves the TLS
    chain + endpoint are good).

    Does not append ``/health`` or parse a status field — the inference origin
    and the bundle host are not health endpoints. A 4xx still means "reachable";
    a TLS/connection error (e.g. the corporate CA is not trusted) is a FAIL,
    which is the signal we want.
    """
    if not url.lower().startswith("https://"):
        outcome.add(name, CheckStatus.FAIL,
                    f"{url} is not HTTPS — Cowork rejects non-HTTPS (originPinned)")
        return
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.ConnectionError as e:
        # SSLError (untrusted corporate CA on the egress proxy) is a ConnectionError.
        outcome.add(name, CheckStatus.FAIL, f"unreachable (TLS/connection): {e}")
        return
    except requests.Timeout:
        outcome.add(name, CheckStatus.FAIL, f"timed out after {REQUEST_TIMEOUT}s")
        return
    except requests.RequestException as e:
        outcome.add(name, CheckStatus.FAIL, f"request error: {e}")
        return

    if resp.status_code >= 500:
        outcome.add(name, CheckStatus.OK, f"reachable but HTTP {resp.status_code} from {url}")
    else:
        outcome.add(name, CheckStatus.OK, f"reachable (HTTP {resp.status_code})")


def _check_cowork_config(outcome: VerifyOutcome) -> dict | None:
    """Confirm the Cowork managed config exists and selects gateway (3P) mode.

    Returns the current config dict (for downstream checks) or None when nothing
    is configured.
    """
    from cli import cowork_config  # noqa: PLC0415

    current = cowork_config.read_config()
    location = cowork_config.config_location()
    if not current:
        outcome.add(
            "cowork-config",
            CheckStatus.FAIL,
            f"no Cowork managed config at {location} — run `{prog_name()} setup`",
        )
        return None
    provider = current.get("inferenceProvider")
    if provider != "gateway":
        outcome.add(
            "cowork-config",
            CheckStatus.OK,
            f"inferenceProvider={provider!r} (expected 'gateway') at {location}",
        )
    else:
        keys = sum(1 for k in current if k.startswith("inference"))
        outcome.add("cowork-config", CheckStatus.OK,
                    f"provider=gateway, {keys} inference keys set at {location}")
    return current


def _check_cowork_hklm(outcome: VerifyOutcome) -> None:
    """Flag the HKLM precedence trap (Windows only), scope-aware.

    Any value under HKLM\\SOFTWARE\\Policies\\Claude makes the app ignore the HKCU
    policy entirely (v1.19367.0+). That is a TRAP when a foreign/stale HKLM key
    shadows our per-user policy — but it is the INTENDED state for a machine-wide
    (`setup --scope machine`) install, where HKLM legitimately carries our gateway
    config. So we only fail when HKLM has values that are NOT our own.
    """
    if sys.platform != "win32":
        return
    from cli import cowork_config  # noqa: PLC0415

    if cowork_config.machine_scope_active():
        outcome.add(
            "cowork-hklm",
            CheckStatus.OK,
            "machine-wide (HKLM) gateway policy in force — applies to all users.",
        )
    elif cowork_config.hklm_conflict():
        outcome.add(
            "cowork-hklm",
            CheckStatus.FAIL,
            "HKLM\\SOFTWARE\\Policies\\Claude holds values NOT written by this tool "
            "(stale, MDM-managed, or a different gateway config) — the app IGNORES "
            "the HKCU policy entirely and honours this one instead. Clear HKLM, or "
            "re-run `setup --scope machine` so this tool owns it.",
        )
    else:
        outcome.add("cowork-hklm", CheckStatus.OK, "no HKLM precedence conflict")


def _check_cowork_ca(outcome: VerifyOutcome) -> None:
    """Verify the corporate CA is trusted by the OS store and matches the pin.

    Only a genuine breakage FAILs here: a PEM whose SHA-256 does not match the
    build-time pin (wrong/tampered CA). A correct CA that is not yet installed in
    the OS trust store, or no corporate CA configured for this environment at all,
    is reported OK — if that actually breaks egress TLS, the egress reachability
    check surfaces it as a FAIL.
    """
    from cli import cowork_ca, site_defaults  # noqa: PLC0415

    pem = cowork_ca.resolve_pem()
    if pem is None:
        outcome.add(
            "cowork-ca",
            CheckStatus.OK,
            "no corporate CA configured for this environment",
        )
        return

    expected = site_defaults.expected_ca_sha256()
    if expected:
        actual = cowork_ca.ca_sha256(pem)
        if actual != expected:
            outcome.add(
                "cowork-ca",
                CheckStatus.FAIL,
                f"CA fingerprint does not match the pin — expected {expected}, "
                f"found {actual}",
            )
            return

    result = cowork_ca.check()
    if result.ok:
        outcome.add("cowork-ca", CheckStatus.OK, result.detail)
    else:
        outcome.add(
            "cowork-ca",
            CheckStatus.OK,
            f"{result.detail} — run `{prog_name()} setup` (installs the CA), "
            f"or `{prog_name()} ca restore` then re-run setup",
        )


def _check_cowork_credential(current: dict | None, outcome: VerifyOutcome) -> None:
    """Report the credential channel written into the managed config.

    Cowork's production credential kind is ``helper-script``: the config carries
    an ``inferenceCredentialHelper`` path to the api-key-helper shim, which the app
    invokes to auto-refresh the ~1h Virtual Key. Mirrors Claude Code's
    ``api-key-helper`` check — surfaces the recorded helper path AND confirms the
    binary exists on disk (a stale path is the common setup failure).

    A ``static`` kind (a raw VK embedded in the config) is a dev/field-test path,
    not the shipped default, so it is only noted briefly.
    """
    if not current:
        return
    kind = str(current.get("inferenceCredentialKind", "") or "")
    if kind == "helper-script":
        helper = str(current.get("inferenceCredentialHelper", "") or "")
        if not helper:
            outcome.add(
                "cowork-credential",
                CheckStatus.FAIL,
                "credential kind is helper-script but inferenceCredentialHelper is "
                f"not set — run `{prog_name()} setup`",
            )
            return
        # The path lands verbatim in the managed config; Desktop requires an
        # absolute local path (never quoted — see cli.paths._search_binary).
        helper_path = Path(helper)
        if helper_path.is_file():
            outcome.add("cowork-credential", CheckStatus.OK,
                        f"helper-script — inferenceCredentialHelper found at {helper_path}")
        else:
            found = shutil.which("api-key-helper")
            hint = (f"binary is on PATH at {found} instead — re-run `{prog_name()} setup`"
                    if found else f"binary missing — reinstall then run `{prog_name()} setup`")
            outcome.add(
                "cowork-credential",
                CheckStatus.FAIL,
                f"inferenceCredentialHelper path not found: {helper_path} — {hint}",
            )
    elif kind == "static":
        outcome.add(
            "cowork-credential",
            CheckStatus.OK,
            "credential kind is static (embedded VK, expires ~1h — dev/field-test "
            f"path). Production uses helper-script: re-run `{prog_name()} setup` "
            "without --credential-kind static.",
        )
    else:
        outcome.add(
            "cowork-credential",
            CheckStatus.OK,
            f"inferenceCredentialKind={kind!r} (expected 'helper-script')",
        )


def _check_cowork_otel(current: dict | None, outcome: VerifyOutcome) -> None:
    """Surface the OTLP telemetry keys the managed config carries, if any.

    OTEL is optional for Cowork (injected via site-extra ``otlp*`` keys, not core
    routing), so absence is an OK "not configured" — never a failure. When
    present, list the endpoint/protocol so the operator can confirm telemetry is
    pointed where they expect.
    """
    if not current:
        return
    otlp = {k: v for k, v in current.items() if k.startswith("otlp")}
    if not otlp:
        outcome.add("cowork-otel", CheckStatus.OK,
                    "no OTLP telemetry keys set (optional — configure via site-extra)")
        return
    endpoint = otlp.get("otlpEndpoint")
    protocol = otlp.get("otlpProtocol")
    summary = f"{len(otlp)} OTLP key(s) set"
    if endpoint:
        summary += f"; otlpEndpoint={endpoint}"
    if protocol:
        summary += f", otlpProtocol={protocol}"
    outcome.add("cowork-otel", CheckStatus.OK, summary)


def run_cowork_verify() -> VerifyOutcome:
    """Health check for the Cowork (Claude Desktop) gateway setup.

    Targets the Desktop surfaces: managed-config presence + location, HKLM trap,
    credential channel (helper path on disk), inference + egress reachability,
    corporate-CA trust/pin, corporate-proxy env, OTLP telemetry, and (helper-script
    kind) the shared api-key-helper token/VK health. OIDC/admin values come from
    the build-baked corporate defaults (:mod:`cli.site_defaults`).
    """
    outcome = VerifyOutcome()

    # 1 — managed config present, in gateway mode, and WHERE it lives (registry /
    #     configLibrary) — the Cowork equivalent of Claude Code's settings path.
    current = _check_cowork_config(outcome)

    # 2 — HKLM precedence trap (Windows).
    _check_cowork_hklm(outcome)

    # 3 — credential channel: the inferenceCredentialHelper path (production
    #     helper-script kind) and whether that binary exists on disk. Mirrors
    #     Claude Code's api-key-helper check.
    _check_cowork_credential(current, outcome)

    # 4 — inference base URL reachable over HTTPS (publicly-trusted CloudFront).
    base_url = str((current or {}).get("inferenceGatewayBaseUrl", "") or "")
    if not base_url:
        from cli import site_defaults  # noqa: PLC0415
        base_url = site_defaults.cowork_gateway_url() or ""
    if base_url:
        _check_https_reachable("cowork-inference-url", base_url, outcome)
    else:
        outcome.add("cowork-inference-url", CheckStatus.OK,
                    "inferenceGatewayBaseUrl not configured — skipped")

    # 5 — egress proxy: the mandatory downloads.claude.ai bundle fetch. A TLS
    #     failure here means the corporate CA is not trusted by the OS store.
    _check_https_reachable("cowork-egress", _COWORK_EGRESS_URL, outcome)

    # 6 — corporate CA trust + fingerprint pin (the OS trust store Chromium reads).
    _check_cowork_ca(outcome)

    # 7 — corporate forward-proxy env (HTTP_PROXY/HTTPS_PROXY must equal
    #     managed.EXPECTED_PROXY_URL; NO_PROXY must not contain
    #     managed.FORBIDDEN_NO_PROXY_TOKEN).
    #     Chromium honours the OS proxy env, so the same rule Claude Code enforces
    #     applies to Cowork — reuse the identical check (single source of truth in
    #     cli.managed).
    _check_proxy_settings(outcome)

    # 8 — OTLP telemetry keys carried in the managed config (optional).
    _check_cowork_otel(current, outcome)

    # 9 — helper token health (helper-script kind only; static embeds the VK).
    #     Checks the api-key-helper's own OS data-dir oidc-tokens / vk-cache.
    cred_kind = str((current or {}).get("inferenceCredentialKind", "") or "")
    if cred_kind == "helper-script":
        issuer_url = oidc_issuer_url().rstrip("/")
        client_id = oidc_client_id()
        api_url = admin_api_url().rstrip("/")
        if issuer_url and client_id:
            _check_oidc_tokens(issuer_url, client_id, outcome)
        if issuer_url and api_url:
            _check_vk_cache(issuer_url, api_url, outcome)

    log.info(
        "cowork_verify_complete",
        overall=outcome.overall.value,
        checks={c.name: c.status.value for c in outcome.checks},
    )
    return outcome
