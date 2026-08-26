# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Step 5 — end-to-end health check.

Verifies the full stack from the end-user's machine:

  1. Gateway reachability  — GET <gatewayUrl>/health  → JSON status field
  2. Admin API reachability — GET <adminApiUrl>/health  → any 2xx
  3. OIDC token check      — oidc-tokens.json in OS data dir, not expired
  4. VK cache check        — vk-cache.json in OS data dir, not expired
  5. Claude Code settings  — settings file (platform-aware path) has apiKeyHelper + ANTHROPIC_BASE_URL
  6. Conflicting env vars  — no gateway-bypassing var set anywhere:
                             CLAUDE_CODE_USE_BEDROCK=1 (routes to Bedrock) or
                             ANTHROPIC_API_KEY (shadows apiKeyHelper → "Invalid API key").
                             Driven off cli.reconcile. When found in the OS/system
                             environment, verify prints multi-level (User + System/
                             admin) removal guidance — gateway-cli does NOT modify
                             the user's system environment variables itself.
  7. Proxy settings        — HTTP_PROXY / HTTPS_PROXY equal the corporate proxy
                             (cli.managed.EXPECTED_PROXY_URL) and NO_PROXY does NOT
                             contain the forbidden corporate suffix
                             (cli.managed.FORBIDDEN_NO_PROXY_TOKEN). A user unfamiliar with dev
                             environments can easily miss a broken proxy setup, so
                             verify checks it explicitly and points at the FAQ on a
                             conflict. OS/shell env vars override the settings file,
                             so the effective value is checked (process env first).

Steps 1 and 2 use a short timeout (5 s) so the command fails fast when the gateway
is unreachable. Steps 3–5 are local checks that never make network calls.

Exit codes (propagated via VerifyStepError):
  OK   — all checks pass
  WARN — one or more checks warn (e.g. token expiring soon)
  FAIL — one or more checks failed
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import requests
import structlog

from cli.manifest import ResolvedConfig
from cli.paths import oidc_tokens_path, vk_cache_path
from cli.reconcile import env_conflicts

log = structlog.get_logger(component="cli-v2")

REQUEST_TIMEOUT = 5.0  # seconds

# Shown when a proxy setting conflicts with the required corporate values.
# Placeholder wording — the operator will edit this copy; the FAQ pointer is the
# point. Keep it a single string so the message stays consistent across checks.
PROXY_CONFLICT_FAQ_HINT = (
    "설정이 충돌합니다. FAQ를 확인해주세요. "
    "(Proxy setting conflict — please check the FAQ.)"
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class CheckStatus(Enum):
    OK = "ok"
    WARN = "warn"
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
        if any(c.status == CheckStatus.WARN for c in self.checks):
            return CheckStatus.WARN
        return CheckStatus.OK

    def add(self, name: str, status: CheckStatus, detail: str) -> None:
        self.checks.append(CheckResult(name=name, status=status, detail=detail))


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_url_health(name: str, url: str, outcome: VerifyOutcome) -> None:
    """GET <url>/health and check for a 2xx response or a JSON status field."""
    health_url = f"{url.rstrip('/')}/health"
    try:
        resp = requests.get(health_url, timeout=REQUEST_TIMEOUT)
    except requests.ConnectionError as e:
        outcome.add(name, CheckStatus.FAIL, f"connection refused: {e}")
        return
    except requests.Timeout:
        outcome.add(name, CheckStatus.FAIL, f"timed out after {REQUEST_TIMEOUT}s")
        return
    except requests.RequestException as e:
        outcome.add(name, CheckStatus.FAIL, f"request error: {e}")
        return

    if resp.status_code >= 500:
        outcome.add(name, CheckStatus.FAIL, f"HTTP {resp.status_code} from {health_url}")
        return

    # Try to parse the status field from a JSON response.
    try:
        body = resp.json()
        status_field = body.get("status", "")
        if status_field == "unhealthy":
            outcome.add(name, CheckStatus.FAIL, f"status=unhealthy at {health_url}")
        elif status_field == "degraded":
            outcome.add(name, CheckStatus.WARN, f"status=degraded at {health_url}")
        else:
            detail = f"HTTP {resp.status_code} — status={status_field or 'ok'}"
            outcome.add(name, CheckStatus.OK, detail)
    except ValueError:
        # Not JSON — 2xx is good enough.
        outcome.add(name, CheckStatus.OK, f"HTTP {resp.status_code}")


def _check_oidc_tokens(issuer_url: str, client_id: str, outcome: VerifyOutcome) -> None:
    """Verify cached OIDC tokens exist and are not expired."""
    path = oidc_tokens_path()

    if not path.exists():
        outcome.add(
            "oidc-tokens",
            CheckStatus.FAIL,
            f"no token cache at {path} — run `gateway-cli-v2 login`",
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
            CheckStatus.WARN,
            f"token cache is for a different IDP ({cached_issuer}) — re-login may be needed [{path}]",
        )
        return

    expires_at = float(data.get("expires_at", 0))
    remaining = expires_at - time.time()
    if remaining <= 0:
        outcome.add(
            "oidc-tokens",
            CheckStatus.WARN,
            f"access token is expired — re-login may be needed (api-key-helper uses refresh token) [{path}]",
        )
    elif remaining < 120:
        outcome.add("oidc-tokens", CheckStatus.WARN, f"access token expires in {int(remaining)}s [{path}]")
    else:
        outcome.add("oidc-tokens", CheckStatus.OK, f"valid, expires in {int(remaining)}s [{path}]")


def _check_vk_cache(issuer_url: str, admin_api_url: str, outcome: VerifyOutcome) -> None:
    """Verify the Virtual Key cache exists and is not expired."""
    path = vk_cache_path()

    if not path.exists():
        outcome.add(
            "vk-cache",
            CheckStatus.WARN,
            f"no VK cache at {path} — will be created on next api-key-helper call",
        )
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        outcome.add("vk-cache", CheckStatus.WARN, f"cannot read VK cache: {e} [{path}]")
        return

    cached_issuer = data.get("issuer_url", "")
    cached_api = data.get("admin_api_url", "")
    if cached_issuer and cached_issuer != issuer_url:
        outcome.add(
            "vk-cache",
            CheckStatus.WARN,
            f"VK cache is for a different IDP ({cached_issuer}) — re-login needed [{path}]",
        )
        return
    if cached_api and cached_api.rstrip("/") != admin_api_url.rstrip("/"):
        outcome.add(
            "vk-cache",
            CheckStatus.WARN,
            f"VK cache is for a different admin API ({cached_api}) — re-login needed [{path}]",
        )
        return

    expires_at = float(data.get("expires_at", 0))
    remaining = expires_at - time.time()
    if remaining <= 0:
        outcome.add(
            "vk-cache",
            CheckStatus.WARN,
            f"Virtual Key is expired — will be renewed on next api-key-helper call [{path}]",
        )
    elif remaining < 1800:
        outcome.add("vk-cache", CheckStatus.WARN, f"Virtual Key expires soon ({int(remaining)}s) [{path}]")
    else:
        outcome.add("vk-cache", CheckStatus.OK, f"valid, expires in {int(remaining // 60)}m [{path}]")


def _check_claude_settings(gateway_url: str, outcome: VerifyOutcome) -> None:
    """Verify Claude Code settings file has gateway config."""
    from cli.setup import _user_settings_path  # noqa: PLC0415

    override = os.environ.get("GATEWAY_CLI_SETTINGS_PATH")
    path = Path(override).expanduser() if override else _user_settings_path()

    if not path.exists():
        outcome.add(
            "claude-settings",
            CheckStatus.FAIL,
            f"settings file not found at {path} — run `gateway-cli setup`",
        )
        return

    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        outcome.add("claude-settings", CheckStatus.FAIL, f"cannot read settings file: {e} [{path}]")
        return

    issues = []
    if not settings.get("apiKeyHelper"):
        issues.append("apiKeyHelper missing")
    base_url = (settings.get("env") or {}).get("ANTHROPIC_BASE_URL", "")
    if not base_url:
        issues.append("env.ANTHROPIC_BASE_URL missing")
    elif base_url.rstrip("/") != gateway_url.rstrip("/"):
        issues.append(
            f"env.ANTHROPIC_BASE_URL ({base_url}) does not match configured gatewayUrl ({gateway_url})"
        )

    if issues:
        outcome.add("claude-settings", CheckStatus.WARN, "; ".join(issues) + f" [{path}]")
    else:
        outcome.add(
            "claude-settings",
            CheckStatus.OK,
            f"apiKeyHelper set, ANTHROPIC_BASE_URL={base_url} [{path}]",
        )


def _check_api_key_helper(outcome: VerifyOutcome) -> None:
    """Verify the api-key-helper binary recorded in settings.json actually exists on disk.

    Common failure: setup was run before the wheel was installed (or was installed
    to a different Python/scope), so the path is stale or was never written.
    This is especially common in WSL where --user installs land in ~/.local/bin/
    which may not be on PATH at the time setup runs.
    """
    import shutil  # noqa: PLC0415
    from cli.setup import _user_settings_path  # noqa: PLC0415

    override = os.environ.get("GATEWAY_CLI_SETTINGS_PATH")
    settings_path = Path(override).expanduser() if override else _user_settings_path()

    if not settings_path.exists():
        # Already reported by _check_claude_settings — skip to avoid duplicate noise.
        return

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    recorded = settings.get("apiKeyHelper", "")
    if not recorded:
        # Missing key already caught by _check_claude_settings.
        return

    # setup.py wraps paths containing spaces in double-quotes for cmd.exe on Windows.
    # Strip them before filesystem checks so Path.exists() works correctly.
    recorded_path = Path(recorded.strip('"'))
    if recorded_path.exists():
        outcome.add("api-key-helper", CheckStatus.OK, f"found at {recorded_path}")
        return

    # Binary not at the recorded path — try to find it on PATH as a fallback hint.
    found_on_path = shutil.which("api-key-helper")
    if found_on_path:
        outcome.add(
            "api-key-helper",
            CheckStatus.FAIL,
            f"recorded path not found: {recorded_path} — "
            f"binary exists at {found_on_path} instead. "
            f"Run `gateway-cli setup` to update settings.json.",
        )
    else:
        outcome.add(
            "api-key-helper",
            CheckStatus.FAIL,
            f"recorded path not found: {recorded_path} — "
            f"not on PATH either. "
            f"Reinstall the wheel then run `gateway-cli setup`.",
        )


def _settings_env_block() -> dict:
    """Return the ``env`` block of the user settings.json, or {} if unreadable."""
    try:
        from cli.setup import _user_settings_path  # noqa: PLC0415
        p = _user_settings_path()
        if p.exists():
            s = json.loads(p.read_text(encoding="utf-8-sig"))
            return s.get("env") or {}
    except Exception:
        pass
    return {}


def _os_env_removal_guidance(var: str, sources: list[str]) -> str:
    """Return step-by-step, multi-level guidance for removing ``var`` yourself.

    gateway-cli deliberately does NOT modify the user's system environment
    variables. When ``verify`` finds a gateway-bypassing variable in the OS env
    it prints this guide instead, covering BOTH scopes it can live in — the
    per-User scope (no admin needed) and the machine-wide System scope (needs an
    elevated / admin shell) — because a running Claude Code inherits whichever is
    set, and the user may not know which one holds it. ``sources`` (e.g. "User
    registry", "Machine registry", "shell profile: /path (line N)") is used to
    point at the scope(s) actually detected.

    Returns "" when the variable was found only in files gateway-cli manages
    itself (settings.json), where re-running setup is the fix.
    """
    on_windows = sys.platform == "win32"

    if on_windows:
        detected = []
        if any("User registry" in s for s in sources):
            detected.append("사용자(User)")
        if any("Machine registry" in s for s in sources):
            detected.append("시스템(System)")
        where = (
            f" (현재 {', '.join(detected)} 범위에서 발견됨)" if detected else ""
        )
        return (
            f"\n      {var} 환경변수를 직접 제거해 주세요{where}.\n"
            f"      (Remove {var} yourself using one of the methods below.)\n"
            f"        • 사용자 범위(User scope) — PowerShell에서 실행:\n"
            f"            [Environment]::SetEnvironmentVariable('{var}', $null, 'User')\n"
            f"        • 시스템 범위(System scope) — 관리자 권한 PowerShell에서 실행:\n"
            f"            [Environment]::SetEnvironmentVariable('{var}', $null, 'Machine')\n"
            f"        • 또는 GUI 사용: Win+R → sysdm.cpl → 고급 → 환경 변수에서\n"
            f"          '사용자 변수' 및/또는 '시스템 변수'의 {var} 항목을 삭제하세요.\n"
            f"          (Or via GUI: Win+R → sysdm.cpl → Advanced → Environment Variables.)\n"
            f"        제거 후에는 새 PowerShell 창을 여세요 — 이미 실행 중인 세션은\n"
            f"        다시 시작하기 전까지 기존 값을 그대로 유지합니다.\n"
            f"        (Afterwards open a NEW PowerShell window; running sessions keep the old value.)"
        )

    # POSIX / WSL: point at the file(s) holding the export.
    profile_hits = [s.split("shell profile: ", 1)[1] for s in sources if s.startswith("shell profile: ")]
    lines = [
        f"\n      {var} 환경변수를 직접 제거해 주세요.",
        f"      (Remove {var} yourself.)",
    ]
    if profile_hits:
        lines.append("        • 다음 파일에서 export 줄을 삭제(또는 주석 처리)하세요:")
        lines.append("          (Delete or comment out the export line in:)")
        lines.extend(f"            {h}" for h in profile_hits)
        lines.append(
            "        • 시스템 전역 파일(/etc/profile, /etc/zshrc, /etc/bashrc)은 sudo 권한이 필요합니다.\n"
            "          (System-wide files need sudo to edit.)"
        )
    else:
        lines.append(
            "        • 현재 세션에 export되어 있습니다. 다음 명령으로 위치를 찾으세요:\n"
            "          (It is exported in your current session. Find it with:)\n"
            "            grep -REn '(^|export )" + var + "=' ~/.bashrc ~/.bash_profile "
            "~/.profile ~/.zshrc ~/.zprofile /etc/profile /etc/zshrc /etc/bashrc 2>/dev/null"
        )
        lines.append(
            "        • 일치하는 줄을 제거하세요(/etc/* 파일은 sudo 사용).\n"
            "          (Remove the matching line; use sudo for any /etc/* file.)"
        )
    lines.append(
        "        변경 후에는 셸을 다시 시작하거나 파일을 `source` 하여 적용하세요.\n"
        "        (Then restart your shell or `source` the file so the change takes effect.)"
    )
    return "\n".join(lines)


def _check_conflicting_env(outcome: VerifyOutcome) -> None:
    """Report any gateway-bypassing env var still set anywhere.

    Driven off :data:`cli.reconcile.CONFLICTS` (the single source of truth), so
    verify and setup can never drift. For each conflict we scan the process env,
    settings.json env block, shell profiles (POSIX) and the Windows registry; a
    value that ``is_alarming`` (e.g. CLAUDE_CODE_USE_BEDROCK only when "1",
    ANTHROPIC_API_KEY whenever set) is a FAIL for fatal conflicts, otherwise a
    WARN. When nothing is found the check passes.

    When the conflict lives in the OS/system environment (registry or shell
    profile), the detail includes explicit, multi-level removal guidance (User
    and System/admin scope): gateway-cli does not touch the user's system
    environment variables, so verify tells the user exactly how to remove it and
    confirm the fix themselves.
    """
    settings_env = _settings_env_block()

    for conflict in env_conflicts():
        var = conflict.key
        sources: list[str] = []
        in_settings_only = True  # only found in settings.json (a file we manage)?
        alarming = False

        # Process environment (inherited by Claude Code launched from this shell).
        proc_val = os.environ.get(var)
        if proc_val is not None:
            sources.append("process env")
            in_settings_only = False
            alarming = alarming or conflict.is_alarming(proc_val)

        # settings.json env block (Claude Code injects these at startup).
        if var in settings_env:
            sources.append("settings.json env")
            alarming = alarming or conflict.is_alarming(str(settings_env.get(var)))

        # Shell profiles (POSIX) — a persisted export beats every settings file.
        if sys.platform != "win32":
            profile_hits = _scan_shell_profiles(var)
            sources.extend(f"shell profile: {h}" for h in profile_hits)
            if profile_hits:
                in_settings_only = False
            # Presence in a profile is treated as alarming (we cannot cheaply read
            # the assigned value, and any active export of a conflict is a problem).
            alarming = alarming or bool(profile_hits)

        # Windows registry (Machine + User scope).
        reg_hits = _read_windows_reg_env(var)
        sources.extend(reg_hits)
        if reg_hits:
            in_settings_only = False
        alarming = alarming or bool(reg_hits)

        check_name = f"conflict:{var}"
        if not sources:
            outcome.add(
                check_name,
                CheckStatus.OK,
                f"{var} 환경변수가 설정되어 있지 않습니다 — 충돌 없음. ({var} not set — no conflict.)",
            )
            continue

        if alarming:
            status = CheckStatus.FAIL if conflict.fatal else CheckStatus.WARN
            detail = (
                f"{var} 환경변수가 설정되어 있습니다 ({', '.join(sources)}) — {conflict.reason_ko}. "
                f"({var} is set — {conflict.reason}.)"
            )
            if in_settings_only:
                # Lives only in a file gateway-cli owns — setup clears it.
                detail += (
                    " `gateway-cli setup`을 다시 실행하면 settings.json에서 제거됩니다."
                    " (Re-run `gateway-cli setup` to clear it from settings.json.)"
                )
            else:
                # Lives in the OS/system env — guide the user to remove it (we do
                # NOT modify system environment variables for them). If it is also
                # in settings.json, setup handles that copy.
                if "settings.json env" in sources:
                    detail += (
                        " settings.json에 있는 복사본은 `gateway-cli setup`을 다시 실행하면 제거됩니다."
                        " (Re-run `gateway-cli setup` to clear the settings.json copy.)"
                    )
                detail += _os_env_removal_guidance(var, sources)
            outcome.add(check_name, status, detail)
        else:
            # Present but not at an alarming value (e.g. BEDROCK=0) — informational.
            outcome.add(
                check_name,
                CheckStatus.OK,
                f"{var} 환경변수가 설정되어 있으나 우회 값이 아닙니다 ({', '.join(sources)}). "
                f"({var} set but not at a bypassing value.)",
            )


def _effective_proxy_env() -> dict[str, str]:
    """Return the effective proxy vars, OS/process env winning over settings.

    An actual OS/shell environment variable overrides any value in the managed
    settings file (Claude Code / OS behavior — see cli.managed), so verify must
    check what Claude Code will really see: the process env value when set, else
    the value written into the managed-settings ``env`` block.
    """
    from cli.managed import read_gateway_settings  # noqa: PLC0415

    managed_env = (read_gateway_settings() or {}).get("env") or {}
    effective: dict[str, str] = {}
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        os_val = os.environ.get(var)
        effective[var] = os_val if os_val is not None else str(managed_env.get(var, ""))
    return effective


def _check_proxy_settings(outcome: VerifyOutcome) -> None:
    """Validate the corporate proxy config a non-technical user might miss.

    On the isolated network, HTTP_PROXY / HTTPS_PROXY must equal the corporate
    forward proxy (``cli.managed.EXPECTED_PROXY_URL``) and NO_PROXY must NOT
    contain ``cli.managed.FORBIDDEN_NO_PROXY_TOKEN`` — the
    gateway/OIDC endpoints live under that suffix but are reached via the internal
    CIDR ranges, so listing it as direct breaks routing. A mismatch is a WARN (so
    onboarding still completes with warnings) carrying the FAQ pointer.
    """
    from cli.managed import EXPECTED_PROXY_URL, FORBIDDEN_NO_PROXY_TOKEN  # noqa: PLC0415

    # The corporate-proxy topology is opt-in: it is only baked on the isolated
    # network. When it was not baked (bare build — both constants blank), there is
    # nothing to assert, so skip the check entirely rather than warn on every box
    # off the corporate network. (A blank FORBIDDEN token would also make the
    # ``in`` test below always true — ``"" in s`` is always True — so this guard is
    # load-bearing, not just cosmetic.)
    if not EXPECTED_PROXY_URL and not FORBIDDEN_NO_PROXY_TOKEN:
        return

    env = _effective_proxy_env()

    if EXPECTED_PROXY_URL:
        for var in ("HTTP_PROXY", "HTTPS_PROXY"):
            value = (env.get(var) or "").strip()
            if value == EXPECTED_PROXY_URL:
                outcome.add(f"proxy:{var}", CheckStatus.OK, f"{var}={value}")
            else:
                shown = value or "(미설정 / not set)"
                outcome.add(
                    f"proxy:{var}",
                    CheckStatus.WARN,
                    f"{var}={shown} — {EXPECTED_PROXY_URL} 이어야 합니다 "
                    f"(expected {EXPECTED_PROXY_URL}). {PROXY_CONFLICT_FAQ_HINT}",
                )

    if not FORBIDDEN_NO_PROXY_TOKEN:
        return

    no_proxy = (env.get("NO_PROXY") or "").strip()
    if FORBIDDEN_NO_PROXY_TOKEN in no_proxy:
        outcome.add(
            "proxy:NO_PROXY",
            CheckStatus.WARN,
            f"NO_PROXY에 '{FORBIDDEN_NO_PROXY_TOKEN}'가 포함되어 있습니다 "
            f"('{FORBIDDEN_NO_PROXY_TOKEN}' must not be in NO_PROXY): {no_proxy}. "
            f"{PROXY_CONFLICT_FAQ_HINT}",
        )
    else:
        shown = no_proxy or "(미설정 / not set)"
        outcome.add("proxy:NO_PROXY", CheckStatus.OK, f"NO_PROXY={shown}")


_GATEWAY_ENV_VARS = [
    "OIDC_ISSUER_URL",
    "OIDC_CLIENT_ID",
    "ADMIN_API_URL",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "ANTHROPIC_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
]


def _shell_profile_paths() -> list[Path]:
    """Return shell profile files that export env vars on macOS/Linux.

    Covers both per-user rc files and the system-wide files a bypass variable can
    be exported from. A definition in any of these is inherited by a freshly
    launched Claude Code even when it is absent from the current ``verify`` shell,
    so verify must scan them all — otherwise a persistent conflict passes cleanly
    yet still defeats the gateway on the next launch.
    """
    home = Path.home()
    candidates = [
        home / ".bashrc",
        home / ".bash_profile",
        home / ".profile",
        home / ".zshrc",
        home / ".zprofile",
        home / ".config" / "fish" / "config.fish",
    ]
    # System-wide files. These live on both macOS and Linux/WSL — an earlier
    # revision guarded them behind sys.platform == "darwin", which let a bypass
    # var exported from /etc on Linux/WSL slip past verify entirely.
    system_files = [
        Path("/etc/zshrc"),
        Path("/etc/zshenv"),
        Path("/etc/profile"),
        Path("/etc/bashrc"),
        Path("/etc/bash.bashrc"),   # Debian/Ubuntu system bashrc
        Path("/etc/environment"),   # PAM-loaded KEY=VALUE (no `export`) — read by every login
    ]
    # /etc/profile.d/*.sh — the conventional drop-in dir sourced by /etc/profile.
    profile_d = Path("/etc/profile.d")
    if sys.platform != "win32":
        candidates += system_files
        try:
            candidates += sorted(profile_d.glob("*.sh"))
        except OSError:
            pass
    return [p for p in candidates if p.exists()]


def _scan_shell_profiles(var: str) -> list[str]:
    """Return a list of 'file (line N)' strings where `var` is set/exported.

    Matches the assignment syntaxes a persistent definition can use:
      * POSIX/bash/zsh ``export VAR=`` or bare ``VAR=`` (also /etc/environment's
        ``VAR=value`` PAM form, which has no ``export``);
      * fish ``set -x VAR`` / ``set --export VAR`` / ``set -gx VAR`` in
        config.fish, which the earlier ``export VAR=`` regex never recognised.
    """
    posix_pattern = re.compile(
        r"^\s*(?:export\s+)?" + re.escape(var) + r"\s*=",
    )
    # fish: `set` with an export flag (-x / --export, possibly combined like -gx)
    # then the variable name. Flags precede the name, e.g. `set -gx VAR value`.
    fish_pattern = re.compile(
        r"^\s*set\s+(?:-\S*x\S*|--export)(?:\s+-\S+)*\s+" + re.escape(var) + r"\b",
    )
    hits = []
    for path in _shell_profile_paths():
        is_fish = path.name == "config.fish"
        try:
            for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                matched = fish_pattern.match(line) if is_fish else posix_pattern.match(line)
                if matched:
                    hits.append(f"{path} (line {lineno})")
        except OSError:
            pass
    return hits


def _read_windows_reg_env(var: str) -> list[str]:
    """Return registry scopes where `var` is set (Windows only)."""
    sources = []
    if sys.platform != "win32":
        return sources
    try:
        import winreg  # noqa: PLC0415
        # Each scope lives under a DIFFERENT subkey: System/Machine vars are under
        # Session Manager, but User vars are under the short HKCU\Environment key
        # (the same one env.py writes to and removes from). Using the Session
        # Manager path for HKCU queries a nonexistent subkey and silently misses
        # every user-scope var — so pair each hive with its correct subkey.
        for hive, subkey, scope in (
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                "Machine registry",
            ),
            (winreg.HKEY_CURRENT_USER, "Environment", "User registry"),
        ):
            try:
                key = winreg.OpenKey(hive, subkey)
                winreg.QueryValueEx(key, var)
                sources.append(scope)
                winreg.CloseKey(key)
            except FileNotFoundError:
                pass
    except Exception:
        pass
    return sources


# Env vars whose value is a secret and must never be printed verbatim.
_SECRET_ENV_VARS = {"ANTHROPIC_API_KEY"}


def _display_env_value(var: str, value) -> str:
    """repr() a value for display, masking secrets so verify output is safe to paste."""
    if var in _SECRET_ENV_VARS:
        return "'***MASKED***'"
    return repr(value)


def _check_env_vars(outcome: VerifyOutcome) -> None:
    """Report where each gateway-related env var is defined (process env, settings.json, shell profiles, registry)."""
    try:
        from cli.setup import _user_settings_path  # noqa: PLC0415
        settings_path = _user_settings_path()
        settings_env: dict = {}
        if settings_path.exists():
            try:
                s = json.loads(settings_path.read_text(encoding="utf-8-sig"))
                settings_env = (s.get("env") or {})
            except Exception:
                pass
    except Exception:
        settings_path = None
        settings_env = {}

    for var in _GATEWAY_ENV_VARS:
        sources = []

        # Process environment
        val = os.environ.get(var)
        if val is not None:
            sources.append(f"process env = {_display_env_value(var, val)}")

        # settings.json env block
        if var in settings_env:
            label = str(settings_path) if settings_path else "settings.json"
            sources.append(f"settings.json env = {_display_env_value(var, settings_env[var])} [{label}]")

        # Shell profiles (macOS / Linux)
        if sys.platform != "win32":
            for hit in _scan_shell_profiles(var):
                sources.append(f"shell profile: {hit}")

        # Windows registry
        for scope in _read_windows_reg_env(var):
            sources.append(scope)

        if sources:
            detail = "; ".join(sources)
            if var == "CLAUDE_CODE_USE_BEDROCK" and os.environ.get(var) == "1":
                status = CheckStatus.WARN
            elif var == "CLAUDE_CODE_USE_BEDROCK" and settings_env.get(var) == "1":
                status = CheckStatus.WARN
            else:
                status = CheckStatus.OK
            outcome.add(f"env:{var}", status, detail)
        else:
            outcome.add(f"env:{var}", CheckStatus.OK, "not set")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_verify(cfg: ResolvedConfig) -> VerifyOutcome:
    """Run all health checks and return a VerifyOutcome."""
    outcome = VerifyOutcome()

    gateway_url = cfg.gateway_url.rstrip("/")
    admin_api_url = cfg.admin_api_url.rstrip("/")
    issuer_url = cfg.oidc_issuer_url.rstrip("/")
    client_id = cfg.oidc_client_id

    # Check 1 — gateway proxy
    if gateway_url:
        _check_url_health("gateway-proxy", gateway_url, outcome)
    else:
        outcome.add("gateway-proxy", CheckStatus.FAIL, "gatewayUrl not configured")

    # Check 2 — admin API
    if admin_api_url:
        _check_url_health("admin-api", admin_api_url, outcome)
    else:
        outcome.add("admin-api", CheckStatus.FAIL, "adminApiUrl not configured")

    # Check 3 — OIDC tokens
    if issuer_url and client_id:
        _check_oidc_tokens(issuer_url, client_id, outcome)
    else:
        outcome.add(
            "oidc-tokens",
            CheckStatus.WARN,
            "oidcIssuerUrl or oidcClientId not configured — skipped",
        )

    # Check 4 — VK cache
    if issuer_url and admin_api_url:
        _check_vk_cache(issuer_url, admin_api_url, outcome)
    else:
        outcome.add("vk-cache", CheckStatus.WARN, "oidcIssuerUrl or adminApiUrl missing — skipped")

    # Check 5 — Claude Code settings
    if gateway_url:
        _check_claude_settings(gateway_url, outcome)
    else:
        outcome.add("claude-settings", CheckStatus.WARN, "gatewayUrl not configured — skipped")

    # Check 6 — api-key-helper binary exists at the path recorded in settings.json
    _check_api_key_helper(outcome)

    # Check 7 — no gateway-bypassing env var set (CLAUDE_CODE_USE_BEDROCK,
    # ANTHROPIC_API_KEY, …). Driven off cli.reconcile so it stays in lockstep
    # with what setup removes.
    _check_conflicting_env(outcome)

    # Check 8 — corporate proxy settings (HTTP_PROXY/HTTPS_PROXY value + NO_PROXY
    # must not contain FORBIDDEN_NO_PROXY_TOKEN). Constants centralized in cli.managed so
    # verify and setup can never drift.
    _check_proxy_settings(outcome)

    # Check 9 — env var source map (process env, settings.json, shell profiles, registry)
    _check_env_vars(outcome)

    log.info(
        "verify_complete",
        overall=outcome.overall.value,
        checks={c.name: c.status.value for c in outcome.checks},
    )
    return outcome
