# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Single source of truth for configuration that CONFLICTS with gateway routing.

``gateway-cli setup`` is otherwise purely additive — it writes its own keys but
never touches anything else. That leaves stale config from a prior Bedrock-based
or direct-Anthropic install in place, and some of it silently defeats the
gateway even after a "successful" setup:

  * ``CLAUDE_CODE_USE_BEDROCK=1`` routes Claude Code straight to Bedrock,
    bypassing the gateway entirely. A real OS/shell env var of this name beats
    every settings file (managed included), so it must be swept from the OS env
    as well as settings.json.
  * ``ANTHROPIC_API_KEY`` is honoured by Claude Code in preference to
    ``apiKeyHelper`` (see ``statusline/main.py`` — "ANTHROPIC_API_KEY env >
    api-key-helper binary"). A stale/invalid value is sent verbatim to the
    gateway and rejected with "Invalid API key · Fix external API key", even
    though ``apiKeyHelper`` is correctly configured.
  * A user-tier ``statusLine`` and a legacy ``modelOverrides`` block linger in
    ``~/.claude/settings.json`` after setup starts managing those concerns at
    the managed tier, producing confusing / conflicting behaviour.

This module declares — in ONE place — every such key, which layer it lives in,
and a human-readable reason. ``setup`` consumes it to REMOVE the keys from the
``settings.json`` file it manages (after the existing timestamped backup);
``verify`` consumes it to REPORT any that are still set — including as an OS/
system environment variable — and to print removal guidance. Adding or retiring
a conflict is a one-line edit here, with no change to either consumer.

The *fact* of which keys are gateway-bypassing is single-sourced from the
declarative catalog in ``cli.manifest`` (``Status.BYPASS``); this module owns the
operational *policy* for each (layers to sweep, fatal/alarm rules, localized
reason). :func:`manifest_coverage_gaps` cross-checks the two so they cannot drift
(enforced by the drift-guard test), with ``statusLine`` the one documented
exception (manifest-OWNED at the managed tier, stripped here only from the stale
user tier — see :data:`MANIFEST_STRIP_ONLY_KEYS`).

By deliberate design this module (and ``setup``) NEVER mutates an OS/system
environment variable: a conflict living in the OS env (``Layer.OS_ENV``) is
reported by ``verify`` with step-by-step removal guidance so the user removes it
themselves at the correct (User or System) scope. This module only decides
*what* is a conflict and, via :func:`reconcile_settings`, edits the in-memory
``settings.json`` dict — never the shell rc or the Windows registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cli import manifest


class Layer(Enum):
    """Where a conflicting key physically lives."""

    #: A key inside the ``env`` object of ``~/.claude/settings.json``.
    SETTINGS_ENV = "settings.json env"
    #: A top-level key of ``~/.claude/settings.json``.
    SETTINGS_TOP = "settings.json (top-level)"
    #: A permanent OS environment variable (shell rc on POSIX / HKCU on Windows)
    #: and/or the current process environment.
    OS_ENV = "OS environment"


@dataclass(frozen=True)
class Conflict:
    """One key that conflicts with gateway routing and must be reconciled.

    key:      the setting / variable name.
    layers:   every layer the key must be swept from.
    reason:   why it breaks the gateway, in English (shown by ``verify`` and
              logged by setup).
    reason_ko: the same explanation in Korean, shown to end users by ``verify``
              alongside the English text.
    fatal:    when True, ``verify`` reports a detected value as FAIL (actively
              breaks routing); when False, WARN (stale / confusing but not
              gateway-breaking).
    alarm_value: when set, only this exact value is a problem for ``verify`` —
              e.g. ``CLAUDE_CODE_USE_BEDROCK`` matters only when ``"1"``. When
              None, any present/non-empty value triggers. NOTE: removal by
              ``setup`` ignores this and always strips the key when present
              (a leftover ``BEDROCK=0`` is harmless noise worth clearing too).
    """

    key: str
    layers: tuple[Layer, ...]
    reason: str
    reason_ko: str = ""
    fatal: bool = True
    alarm_value: str | None = None

    def is_alarming(self, value: str | None) -> bool:
        """Whether an observed ``value`` for this key warrants a ``verify`` alarm."""
        if value is None:
            return False
        if self.alarm_value is not None:
            return value == self.alarm_value
        return value != ""


# ── The single source of truth ──────────────────────────────────────────────
# Add or retire a conflict here; setup (remove) and verify (report) both follow.
CONFLICTS: tuple[Conflict, ...] = (
    Conflict(
        key="CLAUDE_CODE_USE_BEDROCK",
        layers=(Layer.OS_ENV, Layer.SETTINGS_ENV),
        reason=(
            "routes Claude Code directly to Bedrock, bypassing the gateway "
            "(an OS/shell env var of this name overrides even managed settings)"
        ),
        reason_ko=(
            "Claude Code를 게이트웨이를 우회하여 Bedrock으로 직접 라우팅합니다"
            "(이 이름의 OS/셸 환경변수는 관리 설정(managed settings)보다도 우선합니다)"
        ),
        fatal=True,
        alarm_value="1",
    ),
    Conflict(
        key="ANTHROPIC_BEDROCK_BASE_URL",
        layers=(Layer.OS_ENV, Layer.SETTINGS_ENV),
        reason=(
            "leftover from a direct-Bedrock install; points Claude Code at a "
            "Bedrock endpoint instead of the gateway when CLAUDE_CODE_USE_BEDROCK "
            "is also set (an OS/shell env var of this name overrides settings)"
        ),
        reason_ko=(
            "Bedrock 직접 설치에서 남은 값입니다. CLAUDE_CODE_USE_BEDROCK가 함께 설정된 "
            "경우 Claude Code를 게이트웨이 대신 Bedrock 엔드포인트로 향하게 합니다"
            "(이 이름의 OS/셸 환경변수는 설정(settings)보다 우선합니다)"
        ),
        # Harmless on its own (only routes to Bedrock alongside
        # CLAUDE_CODE_USE_BEDROCK=1), but always stripped by setup as stale config.
        fatal=False,
    ),
    Conflict(
        key="ANTHROPIC_API_KEY",
        layers=(Layer.OS_ENV, Layer.SETTINGS_ENV),
        reason=(
            "takes precedence over apiKeyHelper — a stale/invalid value is sent "
            "to the gateway and rejected as 'Invalid API key · Fix external API key'"
        ),
        reason_ko=(
            "apiKeyHelper보다 우선합니다 — 오래되었거나 잘못된 값이 게이트웨이로 전송되어 "
            "'Invalid API key · Fix external API key' 오류로 거부됩니다"
        ),
        fatal=True,
    ),
    Conflict(
        key="statusLine",
        layers=(Layer.SETTINGS_TOP,),
        reason=(
            "the gateway now manages statusLine at the managed tier; a stale "
            "user-tier statusLine points at an old script and can shadow it"
        ),
        reason_ko=(
            "이제 게이트웨이가 statusLine을 관리 계층(managed tier)에서 관리합니다. "
            "오래된 사용자 계층 statusLine은 예전 스크립트를 가리켜 이를 가릴 수 있습니다"
        ),
        fatal=False,
    ),
    Conflict(
        key="modelOverrides",
        layers=(Layer.SETTINGS_TOP,),
        reason=(
            "legacy model-routing key superseded by managed 'model' / "
            "'availableModels' / ANTHROPIC_DEFAULT_* — leaving it causes "
            "conflicting model selection"
        ),
        reason_ko=(
            "관리 설정의 'model' / 'availableModels' / ANTHROPIC_DEFAULT_*로 대체된 "
            "레거시 모델 라우팅 키입니다 — 남겨두면 모델 선택이 충돌합니다"
        ),
        fatal=False,
    ),
)


# ── Derived views (so consumers never re-filter the list by hand) ─────────────

def settings_env_keys() -> list[str]:
    """Keys to remove from the ``env`` block of settings.json."""
    return [c.key for c in CONFLICTS if Layer.SETTINGS_ENV in c.layers]


def settings_top_keys() -> list[str]:
    """Top-level keys to remove from settings.json."""
    return [c.key for c in CONFLICTS if Layer.SETTINGS_TOP in c.layers]


def env_conflicts() -> list[Conflict]:
    """Conflicts that can appear as an environment variable (OS env or settings env).

    These are the ones ``verify`` scans across the process env, settings.json
    env block, shell profiles and the Windows registry — a value present in any
    of those sources bypasses the gateway.
    """
    return [c for c in CONFLICTS if Layer.OS_ENV in c.layers or Layer.SETTINGS_ENV in c.layers]


def conflict_for(key: str) -> Conflict | None:
    """Return the :class:`Conflict` for ``key``, or None if it is not tracked."""
    for c in CONFLICTS:
        if c.key == key:
            return c
    return None


# ── Manifest cross-check (single-source the bypass-key *fact*) ────────────────
# The manifest (cli.manifest) is the declarative catalog of which keys are
# gateway-bypassing (Status.BYPASS). This module owns the *policy* for each — the
# layers to sweep, the fatal/alarm rules, and the localized reason — but it must
# not independently decide *which* keys are bypass-class. Every manifest BYPASS
# key therefore has to appear in CONFLICTS, enforced by the drift-guard test
# (tests/test_reconcile_manifest_drift.py) via :func:`manifest_coverage_gaps`.
#
# `statusLine` is the one intentional extra: the manifest classes it OWNED at the
# managed tier, and reconcile strips only a stale *user-tier* copy so the managed
# one wins. It is a "strip stale lower-tier copy" case, not a bypass key.
MANIFEST_STRIP_ONLY_KEYS: frozenset[str] = frozenset({"statusLine"})


def manifest_coverage_gaps() -> tuple[list[str], list[str]]:
    """Cross-check :data:`CONFLICTS` against the manifest's BYPASS catalog.

    Returns ``(missing, unexpected)``:

      * ``missing``    — manifest BYPASS keys with no :class:`Conflict` here, so
                         reconcile would silently fail to strip a gateway-bypassing
                         key the catalog knows about.
      * ``unexpected`` — CONFLICTS keys that are neither a manifest BYPASS key nor
                         a documented strip-only key (:data:`MANIFEST_STRIP_ONLY_KEYS`),
                         i.e. drift in the other direction.

    Both empty ⇒ the two modules agree. This is intentionally a plain function,
    not an import-time assertion: a mismatch is a build-time bug the drift-guard
    test catches, and must never crash the shipped CLI for an end user.
    """
    conflict_keys = {c.key for c in CONFLICTS}
    bypass_keys = set(manifest.bypass_keys())
    missing = sorted(bypass_keys - conflict_keys)
    unexpected = sorted(conflict_keys - bypass_keys - MANIFEST_STRIP_ONLY_KEYS)
    return missing, unexpected


# ── settings.json reconciliation (pure, in-memory) ────────────────────────────

def reconcile_settings(settings: dict) -> list[str]:
    """Remove every tracked conflict from an in-memory settings.json dict.

    Mutates ``settings`` in place and returns the list of ``"<layer>: <key>"``
    labels that were actually removed (empty when the file was already clean).
    The caller is responsible for having snapshotted the file first — this
    function only edits the dict.
    """
    removed: list[str] = []

    env = settings.get("env")
    if isinstance(env, dict):
        for key in settings_env_keys():
            if key in env:
                env.pop(key, None)
                removed.append(f"{Layer.SETTINGS_ENV.value}: {key}")
        # Drop an emptied env block so we don't leave `"env": {}` behind.
        if not env:
            settings.pop("env", None)

    for key in settings_top_keys():
        if key in settings:
            settings.pop(key, None)
            removed.append(f"{Layer.SETTINGS_TOP.value}: {key}")

    return removed
