# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Build-time custom-key injection (guideline 1-4).

An operator may need to inject their own managed keys without editing Python.
This module loads an optional ``site_extra.json`` bundled into the build.

Schema (Cowork / Claude Desktop)
--------------------------------
The file is a **flat map** whose top-level keys are Claude Desktop 3P
managed-config keys, merged by ``cowork_config`` into the single OS-native store
(registry / configLibrary)::

    {
      "inferenceCustomHeaders": { "X-Tenant-Id": "acme" },
      "deploymentOrganizationUuid": "…",
      "disableAutoUpdates": "true"
    }

``cowork_config`` allowlists the keys (see ``COWORK_EXTRA_ALLOWLIST`` /
``CORE_OWNED_KEYS`` there): only real 3P keys are accepted, and the
routing/credential keys gateway-cli owns can never be overridden. Read this
schema with :func:`raw_extra`.

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


def raw_extra() -> dict:
    """Return the whole site-extra document as a dict (``{}`` when absent/invalid).

    This is the entry point for the **Cowork flat-map schema**: the top-level keys
    are Claude Desktop 3P managed-config keys. ``cowork_config`` applies the
    allowlist + core-owned-key protection; this loader deliberately does no
    filtering so that layer owns the policy. A document that happens to carry the
    legacy ``managed``/``user`` sections is returned verbatim — those keys are not
    on the 3P allowlist, so ``cowork_config`` will drop them with a warning rather
    than write nonsense into the store.
    """
    return _load_raw()
