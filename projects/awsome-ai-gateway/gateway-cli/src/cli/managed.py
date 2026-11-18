# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Managed settings file management for Claude Code.

Writes gateway configuration to the managed-settings.d drop-in directory,
which has the highest priority in Claude Code's settings hierarchy. The
directory is per OS (https://code.claude.com/docs/en/managed-settings.md):

macOS:     /Library/Application Support/ClaudeCode/managed-settings.d/50-gateway.json
Linux/WSL: /etc/claude-code/managed-settings.d/50-gateway.json
Windows:   C:\\Program Files\\ClaudeCode\\managed-settings.d\\50-gateway.json

Earlier versions wrote /etc/claude-code on macOS too, which Claude Code on
macOS never reads; see _legacy_managed_file for how that file is handled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(component="cli")

GATEWAY_SETTINGS_FILENAME = "50-gateway.json"


def _managed_dir() -> Path:
    """Return the managed-settings.d directory path for the current platform.

    Must be the system directory Claude Code itself reads on this OS. A drop-in
    anywhere else is silently ignored while setup reports success and status
    shows [ON] — which is what happened on macOS when this fell through to /etc.
    """
    if sys.platform == "win32":
        return Path(r"C:\Program Files\ClaudeCode\managed-settings.d")
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.d")
    return Path("/etc/claude-code/managed-settings.d")


def _managed_file() -> Path:
    return _managed_dir() / GATEWAY_SETTINGS_FILENAME


def _legacy_managed_file() -> Path | None:
    """Where earlier versions wrote the drop-in on macOS; None on other OSes.

    Those versions used /etc/claude-code on every non-Windows OS. Claude Code on
    macOS does not read it, so a file there is inert: it never counts as enabled,
    and setup/disable remove it. On Linux/WSL that path is the live location and
    Windows never used it, so neither has a legacy file.
    """
    # Linux 경로를 _managed_dir() 에서 빌려 쓰지 않고 따로 적는다 — 이건 '예전 버전이 실제로
    # 쓴 자리'라는 과거 사실이라, 앞으로 Linux 경로가 바뀌어도 따라 바뀌면 안 된다.
    if sys.platform == "darwin":
        return Path("/etc/claude-code/managed-settings.d") / GATEWAY_SETTINGS_FILENAME
    return None


def find_legacy_gateway_settings() -> Path | None:
    """Return the legacy drop-in if one is present on this machine, else None."""
    legacy = _legacy_managed_file()
    if legacy is not None and legacy.is_file():
        return legacy
    return None


def is_gateway_enabled() -> bool:
    """Check if gateway managed settings file exists.

    Only the location Claude Code reads counts; a legacy file does not.
    """
    return _managed_file().is_file()


def read_gateway_settings() -> dict | None:
    """Read the current gateway managed settings, or None if not present."""
    path = _managed_file()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_gateway_settings(
    gateway_url: str,
    admin_api_url: str,
    api_key_helper_path: str,
    otel_endpoint: str | None = None,
    otel_auth_token: str | None = None,
    oidc_issuer_url: str | None = None,
    oidc_client_id: str | None = None,
    oidc_audience: str | None = None,
) -> dict:
    """Assemble the managed-settings document (pure — no filesystem, no sudo).

    Split out of :func:`write_gateway_settings` so the env block can be unit
    tested without sudo; the write path is a thin wrapper around this.

    gateway_url:   Gateway proxy URL (ANTHROPIC_BASE_URL — for Claude Code API calls)
    admin_api_url: Admin API URL (GATEWAY_CLI_GATEWAY_URL — for api-key-helper VK issuance)

    OIDC_ISSUER_URL / OIDC_CLIENT_ID are written because ``apiKeyHelper`` runs as a
    *child of Claude Code*, so its only configuration channel is this ``env`` block —
    it does not inherit the operator's shell. api_key_helper._detect_mode requires
    BOTH vars to pick OIDC mode; with neither present it silently selects STS(IAM)
    mode, which either issues a VK against the caller's AWS IAM ARN (wrong user —
    admin-api provisions ``<session_name>@unknown``) or, with no SSO session, prints
    nothing to stdout and exits 1. Claude Code then has no credential and falls back
    to its own first-party login prompt ("Not logged in · Please run /login") — i.e.
    the redirect-to-1P symptom. Omitting these two keys was the defect;
    guides/user-guide.md §5.2 has always told manual users to set all four.
    """
    env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": gateway_url,
        "GATEWAY_CLI_GATEWAY_URL": admin_api_url,
    }

    # OIDC (IDP) — api-key-helper 가 OIDC 모드로 동작하기 위한 필수 2개.
    # ADMIN_API_URL 은 별도로 쓰지 않는다: gateway_cli_oidc.load_oidc_config_from_env 가
    # ``ADMIN_API_URL or GATEWAY_CLI_GATEWAY_URL`` 로 폴백하므로 위 키가 이미 그 역할을 한다
    # (같은 값을 두 키로 중복 기록하면 나중에 한쪽만 수정돼 어긋난다).
    if oidc_issuer_url and oidc_client_id:
        env["OIDC_ISSUER_URL"] = oidc_issuer_url.rstrip("/")
        env["OIDC_CLIENT_ID"] = oidc_client_id
        # audience 는 선택 — admin-api 의 OIDC_AUDIENCE 검증이 켜져 있을 때만 필요.
        # 빈 문자열을 쓰면 IDP 마다 audience 해석이 달라 401 을 유발할 수 있어 생략한다.
        if oidc_audience:
            env["OIDC_AUDIENCE"] = oidc_audience

    # Claude Code client-side OTEL (metrics + traces + code activity)
    if otel_endpoint:
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = otel_endpoint
        env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"
        env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        env["OTEL_METRICS_EXPORTER"] = "otlp"
        env["OTEL_TRACES_EXPORTER"] = "otlp"
        env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
        if otel_auth_token:
            env["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Bearer {otel_auth_token}"

    return {
        "_comment": "LLM Gateway — managed by gateway-cli",
        "env": env,
        "apiKeyHelper": api_key_helper_path,
        "statusLine": {"type": "command", "command": "statusline"},
    }


@dataclass(frozen=True)
class GatewayWriteResult:
    """What write_gateway_settings did, so setup can report all of it."""

    path: Path
    # macOS 에서 이전 버전이 남긴 /etc/claude-code 파일을 지웠으면 그 경로, 아니면 None.
    legacy_removed: Path | None = None


def write_gateway_settings(
    gateway_url: str,
    admin_api_url: str,
    api_key_helper_path: str,
    otel_endpoint: str | None = None,
    otel_auth_token: str | None = None,
    oidc_issuer_url: str | None = None,
    oidc_client_id: str | None = None,
    oidc_audience: str | None = None,
) -> GatewayWriteResult:
    """Write gateway managed settings file.

    Requires root/admin — uses sudo on macOS, Linux and WSL.
    On macOS it then removes the legacy file earlier versions left in
    /etc/claude-code (see _legacy_managed_file).
    Returns the written path and the removed legacy path, if any.
    """
    settings = build_gateway_settings(
        gateway_url=gateway_url,
        admin_api_url=admin_api_url,
        api_key_helper_path=api_key_helper_path,
        otel_endpoint=otel_endpoint,
        otel_auth_token=otel_auth_token,
        oidc_issuer_url=oidc_issuer_url,
        oidc_client_id=oidc_client_id,
        oidc_audience=oidc_audience,
    )

    content = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    target = _managed_file()

    if sys.platform == "win32":
        _write_windows(target, content)
    else:
        _write_unix(target, content)

    log.info("managed_settings_written", path=str(target))

    # 새 파일을 쓴 *뒤에* 지운다. 쓰기가 실패하면(sudo 거부 등) 예전 파일이 그대로 남아,
    # status 가 계속 "setup 을 다시 실행하라" 고 안내할 수 있어야 한다.
    legacy = find_legacy_gateway_settings()
    if legacy is not None:
        _remove_unix(legacy)
        log.info("legacy_managed_settings_removed", path=str(legacy))
    return GatewayWriteResult(path=target, legacy_removed=legacy)


def remove_gateway_settings() -> bool:
    """Remove the gateway managed settings file, and on macOS the legacy one.

    Returns True if either existed (and was removed), False if neither did.
    """
    removed = False
    target = _managed_file()
    if target.is_file():
        if sys.platform == "win32":
            _remove_windows(target)
        else:
            _remove_unix(target)
        log.info("managed_settings_removed", path=str(target))
        removed = True

    legacy = find_legacy_gateway_settings()
    if legacy is not None:
        _remove_unix(legacy)
        log.info("legacy_managed_settings_removed", path=str(legacy))
        removed = True

    return removed


# ---------------------------------------------------------------------------
# Platform-specific write/remove (requires elevated privileges)
# ---------------------------------------------------------------------------

def _write_unix(target: Path, content: str) -> None:
    """Write via sudo tee (no temp file needed, atomic enough)."""
    # 인자는 셸 문자열이 아니라 리스트로 넘긴다(shell=False). macOS 경로는
    # "Application Support" 에 공백이 있어, 셸 문자열로 조립하면 공백에서 경로가 두 인자로
    # 쪼개진다.
    parent = target.parent
    # Ensure directory exists
    subprocess.run(
        ["sudo", "mkdir", "-p", str(parent)],
        check=True,
    )
    # Write content via sudo tee
    subprocess.run(
        ["sudo", "tee", str(target)],
        input=content.encode(),
        stdout=subprocess.DEVNULL,
        check=True,
    )
    # Set permissions: root-owned, world-readable
    subprocess.run(["sudo", "chmod", "644", str(target)], check=True)
    group = "wheel" if sys.platform == "darwin" else "root"
    subprocess.run(["sudo", "chown", f"root:{group}", str(target)], check=True)


def _remove_unix(target: Path) -> None:
    subprocess.run(["sudo", "rm", "-f", str(target)], check=True)


def _write_windows(target: Path, content: str) -> None:
    """Write directly — requires running as admin on Windows."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _remove_windows(target: Path) -> None:
    target.unlink(missing_ok=True)
