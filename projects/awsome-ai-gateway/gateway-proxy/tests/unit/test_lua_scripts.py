# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.lua_loader import LuaScriptLoader


def test_load_all_scripts():
    script_dir = Path(__file__).parent.parent.parent / "src" / "app" / "redis_scripts"
    if not script_dir.exists():
        pytest.skip("redis_scripts directory not found")

    LuaScriptLoader.load_all(script_dir)
    assert LuaScriptLoader.is_loaded("rate_limit_check")
    assert LuaScriptLoader.is_loaded("rate_limit_tpm_check")
    assert LuaScriptLoader.is_loaded("cost_rate_limit")
    assert LuaScriptLoader.is_loaded("budget_check")
    assert LuaScriptLoader.is_loaded("budget_deduct")


def test_get_script_not_loaded():
    with pytest.raises(KeyError):
        LuaScriptLoader.get("nonexistent_script")


def test_scripts_contain_keys_argv():
    script_dir = Path(__file__).parent.parent.parent / "src" / "app" / "redis_scripts"
    if not script_dir.exists():
        pytest.skip("redis_scripts directory not found")

    LuaScriptLoader.load_all(script_dir)
    # budget_check.lua uses only KEYS (no ARGV); others use both
    for name in ["rate_limit_check", "rate_limit_tpm_check", "budget_deduct"]:
        script = LuaScriptLoader.get(name)
        assert "KEYS" in script
        assert "ARGV" in script
