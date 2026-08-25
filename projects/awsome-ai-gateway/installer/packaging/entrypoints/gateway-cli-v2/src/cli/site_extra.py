# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Build-time custom-key injection (guideline 1-4).

An operator may need to inject their own keys into Claude Code's settings files
without editing Python. This module loads an optional ``site_extra.json`` that
is bundled into the build and deep-merges its two sections into the settings
files ``setup`` writes:

    {
      "managed": {          // deep-merged into managed-settings.json (highest tier)
        "env": { "HTTPS_PROXY": "http://proxy.example.com:8080" },
        "permissions": { "allow": ["Bash(git*)"] }
      },
      "user": {             // deep-merged into ~/.claude/settings.json
        "env": { "MY_TEAM_FLAG": "1" }
      }
    }

Precedence: the gateway-cli-owned keys (OTEL, ANTHROPIC_BASE_URL, apiKeyHelper,
…) are applied *after* site-extra, so a site-extra value never silently breaks
gateway routing; every other key site-extra provides is merged in, org keys we
do not touch are preserved. All injected keys are recorded under the private
``_gatewayCli`` marker so ``disable`` unmerges them cleanly.

Where the file comes from
-------------------------
1. ``GATEWAY_CLI_SITE_EXTRA`` env var — explicit path (tests / ad-hoc override).
2. ``site_extra.json`` bundled next to this module (copied there at build time
   by ``build.ps1`` from ``packaging/site-extra.json``). Absent in a plain dev
   checkout, in which case injection is simply a no-op.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog

log = structlog.get_logger(component="cli")

_ENV_OVERRIDE = "GATEWAY_CLI_SITE_EXTRA"
_BUNDLED_NAME = "site_extra.json"


def _candidate_paths() -> list[Path]:
    """Ordered locations to look for the site-extra JSON."""
    paths: list[Path] = []
    override = os.environ.get(_ENV_OVERRIDE)
    if override and override.strip():
        paths.append(Path(override).expanduser())
    # Bundled beside this module (PyInstaller keeps the cli package layout).
    paths.append(Path(__file__).with_name(_BUNDLED_NAME))
    return paths


def _load_raw() -> dict:
    """Read the site-extra JSON, or return {} when none is present/usable."""
    for path in _candidate_paths():
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("site_extra_unreadable", path=str(path), error=str(e))
            continue
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            # A malformed site-extra must not silently corrupt settings.
            log.warning("site_extra_invalid_json", path=str(path), error=str(e))
            return {}
        if not isinstance(data, dict):
            log.warning("site_extra_not_object", path=str(path))
            return {}
        log.info("site_extra_loaded", path=str(path))
        return data
    return {}


def deep_merge(base: dict, extra: dict) -> dict:
    """Recursively merge ``extra`` into ``base`` (in place) and return it.

    Nested dicts are merged key-by-key; any non-dict value (including lists)
    replaces the base value. ``extra`` wins on a type conflict.
    """
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _section(name: str) -> dict:
    """Return the requested top-level section ('managed'/'user') as a dict."""
    section = _load_raw().get(name)
    return section if isinstance(section, dict) else {}


def managed_extra() -> dict:
    """Custom keys to deep-merge into managed-settings.json (may be empty)."""
    return _section("managed")


def user_extra() -> dict:
    """Custom keys to deep-merge into ~/.claude/settings.json (may be empty)."""
    return _section("user")


def merge_into(settings: dict, extra: dict) -> list[str]:
    """Deep-merge ``extra`` into ``settings`` and return the top-level keys touched.

    The returned key list lets the caller record them under the ``_gatewayCli``
    marker so a later ``disable`` knows which keys came from us.
    """
    if not extra:
        return []
    deep_merge(settings, extra)
    return sorted(extra.keys())
