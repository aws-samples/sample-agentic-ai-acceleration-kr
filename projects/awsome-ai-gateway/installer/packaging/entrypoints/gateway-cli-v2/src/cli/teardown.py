# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Software-level teardown for gateway-cli — the `clear` command's engine.

Reverts, surface by surface, everything ``setup``/``login`` wrote *outside* the
managed-settings file (which :func:`cli.managed.remove_gateway_settings` already
unwinds marker-wise). Modelled on the shipped Cowork teardown
(``cowork-installer`` tree: ``cowork_config.remove_config``/``sweep_backups``),
adapted to this CLI's surfaces:

  1. user ``~/.claude/settings.json``  — owned keys removed (manifest-driven),
     pre-setup values restored from the earliest ``claude-code`` snapshot.
  2. OS env — Windows ``HKCU\\Environment``: each persisted var is restored to
     its pre-setup value (from the earliest ``gateway-cli-hkcu-env`` snapshot,
     the T6.2 read-before-write record) or deleted when none existed.
     POSIX: the ``env --persist`` marker block's export lines are stripped from
     the shell rc files.
  3. backup sweep — this tool's own timestamped snapshots are deleted, via an
     explicit ownership-prefix allowlist with a directory-escape guard.

Design notes
------------
* Owned-key sets are derived from :mod:`cli.manifest` (``Status.OWNED`` at the
  user tier), the same single source ``setup`` emits from — the writer and the
  eraser cannot drift.
* "Earliest snapshot wins": the first snapshot ``setup``/``env --persist`` ever
  took records the true pre-gateway state; later snapshots capture gateway's own
  values (a re-run overwrites gateway values with gateway values), so restoring
  from the latest would resurrect our own config.
* Every function is per-user and needs no elevation. The managed-settings step
  (which may need admin on Windows) stays in :mod:`cli.managed` and is invoked
  by the command layer, not here.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from cli import manifest
from cli.manifest import Placement, Status, Tier
from cli.paths import data_dir, oidc_tokens_path, vk_cache_path

log = structlog.get_logger(component="teardown")

# Ownership prefixes of every backup file this suite writes (tool_name arguments
# of backup_config/backup_values across cli.setup, cli.managed and cli.env).
# sweep_backups() deletes ONLY files matching these — a backup dir shared with
# another tool (GATEWAY_CLI_BACKUP_DIR) keeps its other tenants' snapshots.
_BACKUP_SWEEP_PATTERNS = (
    "claude-code.*.bak",
    "claude-code-managed.*.bak",
    "claude-code-managed-dropin.*.bak",
    "gateway-cli-hkcu-env.*.json.bak",
)

# The marker comment env.py's _persist_posix() writes above its export block.
_POSIX_MARKER = "# LLM Gateway — added by gateway-cli env --persist"


# ---------------------------------------------------------------------------
# Owned-key derivation (manifest-driven — the eraser mirrors the emitter)
# ---------------------------------------------------------------------------

def owned_user_settings_keys() -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(top_level_keys, env_keys)`` gateway-cli owns in user settings.

    Derived from the manifest exactly the way ``setup`` selects what to emit
    (``Status.OWNED`` outputs targeting ``SETTINGS_TOP``/``SETTINGS_ENV`` at the
    USER or EITHER tier), so removal can never drift from emission.
    """
    top: set[str] = set()
    env: set[str] = set()
    for f in manifest.FIELDS:
        if f.status is not Status.OWNED:
            continue
        for out in f.outputs or ():
            if out.tier not in (Tier.USER, Tier.EITHER):
                continue
            if out.placement is Placement.SETTINGS_TOP:
                top.add(f.key)
            elif out.placement is Placement.SETTINGS_ENV:
                env.add(f.key)
    return frozenset(top), frozenset(env)


# ---------------------------------------------------------------------------
# Snapshot lookup (shared by the settings revert and the HKCU env restore)
# ---------------------------------------------------------------------------

def _backup_dir() -> Path:
    """The same backups dir cli.utils.backup writes to (honours the override)."""
    override = os.environ.get("GATEWAY_CLI_BACKUP_DIR")
    return Path(override) if override else data_dir() / "backups"


def _earliest_snapshot(pattern: str) -> Path | None:
    """The oldest snapshot matching ``pattern`` in the backups dir, or None.

    Backup filenames embed a UTC ``YYYYMMDDTHHMMSS`` timestamp, so a plain
    lexicographic sort is chronological. The earliest snapshot is the pre-setup
    state (see module docstring).
    """
    backup_dir = _backup_dir()
    if not backup_dir.is_dir():
        return None
    candidates = sorted(backup_dir.glob(pattern))
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# 1. user settings.json revert
# ---------------------------------------------------------------------------

@dataclass
class SettingsRevertResult:
    """What :func:`revert_user_settings` did (all lists are key names)."""

    path: str
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.restored)


def _user_settings_path() -> Path:
    # Mirrors cli.setup._user_settings_path (kept private there).
    return Path.home() / ".claude" / "settings.json"


def revert_user_settings(settings_path: Path | None = None) -> SettingsRevertResult:
    """Remove gateway-cli's owned keys from the user ``settings.json``.

    For each owned key: when the earliest ``claude-code`` snapshot holds a prior
    value for it, that value is restored; otherwise the key is removed. Keys we
    never owned are untouched, and the file itself is never deleted (Claude Code
    may own other keys in it). An unparseable file is left alone — refuse to
    guess, report instead.
    """
    path = settings_path or _user_settings_path()
    result = SettingsRevertResult(path=str(path))
    if not path.is_file():
        result.skipped_reason = "settings.json does not exist"
        return result

    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        result.skipped_reason = f"settings.json unreadable/unparseable: {exc}"
        log.warning("settings_unparseable_on_clear", path=str(path), error=str(exc))
        return result
    if not isinstance(settings, dict):
        result.skipped_reason = "settings.json is not a JSON object"
        return result

    # Pre-setup reference (earliest snapshot). Absent → plain removal.
    prior: dict = {}
    snapshot = _earliest_snapshot("claude-code.settings.json.*.bak")
    if snapshot is not None:
        try:
            loaded = json.loads(snapshot.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prior = loaded
        except (json.JSONDecodeError, OSError):
            prior = {}  # unusable snapshot → treat as no prior state

    top_keys, env_keys = owned_user_settings_keys()

    for key in sorted(top_keys):
        if key not in settings:
            continue
        if key in prior:
            settings[key] = prior[key]
            result.restored.append(key)
        else:
            settings.pop(key)
            result.removed.append(key)

    env_block = settings.get("env")
    prior_env = prior.get("env") if isinstance(prior.get("env"), dict) else {}
    if isinstance(env_block, dict):
        for key in sorted(env_keys):
            if key not in env_block:
                continue
            if key in prior_env:
                env_block[key] = prior_env[key]
                result.restored.append(f"env.{key}")
            else:
                env_block.pop(key)
                result.removed.append(f"env.{key}")
        if not env_block:
            settings.pop("env", None)

    if result.changed:
        path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        log.info(
            "user_settings_reverted",
            path=str(path),
            removed=result.removed,
            restored=result.restored,
        )
    return result


# ---------------------------------------------------------------------------
# 2. OS-env restore (HKCU on Windows, shell rc on POSIX)
# ---------------------------------------------------------------------------

@dataclass
class OsEnvRevertResult:
    """What :func:`restore_os_env` did."""

    restored: list[str] = field(default_factory=list)  # prior value put back
    deleted: list[str] = field(default_factory=list)   # our value removed
    rc_files: list[str] = field(default_factory=list)  # POSIX rc files edited
    skipped_reason: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.restored or self.deleted or self.rc_files)


def _restore_windows_env() -> OsEnvRevertResult:
    """Restore-or-delete each persisted var in ``HKCU\\Environment``.

    The earliest ``gateway-cli-hkcu-env`` snapshot (env.py's T6.2 read-before-
    write record) holds only the vars that HAD a prior value; a var absent from
    it did not exist pre-setup, so it is deleted. With no snapshot at all,
    nothing existed before the first persist — everything we own is deleted.
    """
    import winreg  # noqa: PLC0415 — platform-only import

    result = OsEnvRevertResult()
    prior: dict[str, str] = {}
    snapshot = _earliest_snapshot("gateway-cli-hkcu-env.Environment.*.json.bak")
    if snapshot is not None:
        try:
            loaded = json.loads(snapshot.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prior = {k: v for k, v in loaded.items() if isinstance(v, str)}
        except (json.JSONDecodeError, OSError):
            prior = {}

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
    ) as key:
        for var in manifest.os_persisted_keys():
            try:
                winreg.QueryValueEx(key, var)
            except FileNotFoundError:
                continue  # not set — nothing to revert for this var
            if var in prior:
                winreg.SetValueEx(key, var, 0, winreg.REG_SZ, prior[var])
                result.restored.append(var)
            else:
                winreg.DeleteValue(key, var)
                result.deleted.append(var)
    return result


def _strip_posix_rc(rc_path: Path) -> bool:
    """Remove the persist marker block's lines from one rc file.

    Deletes the marker comment and any ``export VAR="..."`` line for a var we
    persist (the exact shape _persist_posix writes). Lines the user wrote for
    other vars — or hand-written lines for our vars in a different shape — are
    left alone. Returns True when the file was modified.
    """
    if not rc_path.is_file():
        return False
    text = rc_path.read_text(encoding="utf-8")
    our_vars = set(manifest.os_persisted_keys())
    export_re = re.compile(
        r"^export (" + "|".join(re.escape(v) for v in sorted(our_vars)) + r')="[^"]*"$'
    )

    kept: list[str] = []
    changed = False
    for line in text.splitlines():
        if line.strip() == _POSIX_MARKER or export_re.match(line.strip()):
            changed = True
            continue
        kept.append(line)
    if changed:
        new_text = "\n".join(kept)
        if text.endswith("\n"):
            new_text += "\n"
        rc_path.write_text(new_text, encoding="utf-8")
    return changed


def restore_os_env() -> OsEnvRevertResult:
    """Revert the persisted OS environment variables on this platform."""
    if sys.platform == "win32":
        return _restore_windows_env()
    result = OsEnvRevertResult()
    for rc_name in (".zshrc", ".bashrc"):
        rc_path = Path.home() / rc_name
        if _strip_posix_rc(rc_path):
            result.rc_files.append(str(rc_path))
    return result


# ---------------------------------------------------------------------------
# 3. backup sweep (strictly LAST — restores above consume the snapshots)
# ---------------------------------------------------------------------------

def sweep_backups() -> list[Path]:
    """Delete this tool's own backup snapshots. Allowlist + escape-guarded.

    Only files matching :data:`_BACKUP_SWEEP_PATTERNS` inside the backups dir
    are removed — the ownership prefixes scope the sweep to files this suite
    wrote even when GATEWAY_CLI_BACKUP_DIR points at a shared directory, and the
    ``relative_to`` check refuses anything a symlink/glob resolves outside the
    dir. Best-effort: a file that refuses to delete is skipped, not raised.

    Must run AFTER the restores above — they read these snapshots.
    """
    removed: list[Path] = []
    backup_dir = _backup_dir()
    if not backup_dir.is_dir():
        return removed
    for pattern in _BACKUP_SWEEP_PATTERNS:
        for path in sorted(backup_dir.glob(pattern)):
            try:
                path.resolve().relative_to(backup_dir.resolve())
            except ValueError:
                continue  # escaped the backups dir — never touch it
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                log.debug("sweep_backup_failed", path=str(path), error=str(exc))
    return removed


# ---------------------------------------------------------------------------
# 4. post-teardown verification (T7 — did clear actually clear?)
# ---------------------------------------------------------------------------

def post_teardown_checks() -> dict[str, str]:
    """Assert every surface `clear` owns is gone/reverted. Returns {check: status}.

    Statuses: ``ok`` (surface clean) / ``residue`` (something we own remains).
    Read-only — safe to run any time. The managed-settings check reuses
    cli.managed's marker detection so an org-owned file without our marker
    counts as clean.
    """
    from cli.login import load_tokens, load_vk_cache  # noqa: PLC0415 — lazy, mirrors main.py
    from cli.managed import is_gateway_enabled  # noqa: PLC0415

    checks: dict[str, str] = {}

    checks["managed-settings"] = "residue" if is_gateway_enabled() else "ok"

    top_keys, env_keys = owned_user_settings_keys()
    settings_status = "ok"
    path = _user_settings_path()
    if path.is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            settings = {}
        if isinstance(settings, dict):
            leftovers = [k for k in top_keys if k in settings]
            env_block = settings.get("env")
            if isinstance(env_block, dict):
                leftovers += [f"env.{k}" for k in env_keys if k in env_block]
            if leftovers:
                settings_status = "residue"
    checks["user-settings"] = settings_status

    if sys.platform == "win32":
        import winreg  # noqa: PLC0415

        env_status = "ok"
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_QUERY_VALUE
            ) as key:
                for var in manifest.os_persisted_keys():
                    try:
                        winreg.QueryValueEx(key, var)
                        env_status = "residue"
                        break
                    except FileNotFoundError:
                        continue
        except OSError:
            pass
        checks["os-env"] = env_status
    else:
        env_status = "ok"
        for rc_name in (".zshrc", ".bashrc"):
            rc_path = Path.home() / rc_name
            if rc_path.is_file() and _POSIX_MARKER in rc_path.read_text(encoding="utf-8"):
                env_status = "residue"
        checks["os-env"] = env_status

    checks["tokens"] = (
        "residue" if (load_tokens() is not None or load_vk_cache() is not None) else "ok"
    )
    # Raw cache files (a corrupt cache loads as None but still lingers on disk).
    if oidc_tokens_path().exists() or vk_cache_path().exists():
        checks["tokens"] = "residue"

    backup_dir = _backup_dir()
    residue = False
    if backup_dir.is_dir():
        for pattern in _BACKUP_SWEEP_PATTERNS:
            if any(backup_dir.glob(pattern)):
                residue = True
                break
    checks["backups"] = "residue" if residue else "ok"

    return checks
