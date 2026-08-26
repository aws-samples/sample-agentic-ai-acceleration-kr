# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Backwards-compatible alias for the Cowork managed-config writer.

The writer now lives in :mod:`cli.managed`; this module is a thin re-export so
existing importers of ``cli.cowork_config`` keep working. Prefer importing from
:mod:`cli.managed` in new code.

Everything re-exported here — including the private helpers the tests reach for
— is the SAME object as in ``cli.managed``, so ``monkeypatch.setattr`` on this
module still patches the name ``cli.main`` resolves (it imports the module
object, not the function).
"""

from __future__ import annotations

from cli.managed import (
    COWORK_EXTRA_ALLOWLIST,
    ConfigResult,
    ConfigWritePlan,
    CoworkConfig,
    _all_managed_key_names,
    _extra_value_for_store,
    _mac_applied_config_path_readonly,
    _policy_values,
    build_config,
    config_location,
    hklm_conflict,
    installer_registry_scope,
    is_gateway_enabled,
    machine_scope_active,
    marker_exists,
    plan_write,
    read_config,
    remove_config,
    resolve_registry_scope,
    resolve_site_extra,
    sweep_backups,
    write_config,
)

__all__ = [
    "COWORK_EXTRA_ALLOWLIST",
    "ConfigResult",
    "ConfigWritePlan",
    "CoworkConfig",
    "build_config",
    "config_location",
    "hklm_conflict",
    "installer_registry_scope",
    "is_gateway_enabled",
    "machine_scope_active",
    "marker_exists",
    "plan_write",
    "read_config",
    "remove_config",
    "resolve_registry_scope",
    "resolve_site_extra",
    "sweep_backups",
    "write_config",
    # Private helpers re-exported for the test suite / internal callers.
    "_all_managed_key_names",
    "_extra_value_for_store",
    "_mac_applied_config_path_readonly",
    "_policy_values",
]
