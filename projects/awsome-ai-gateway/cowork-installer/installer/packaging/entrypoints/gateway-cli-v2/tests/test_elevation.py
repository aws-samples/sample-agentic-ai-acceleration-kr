# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""UAC self-elevation gating and relaunch-argv construction for ``setup``.

The Win32 calls themselves (ShellExecuteExW / token queries) are Windows-only and
proven on the box; here we drive the pure decision logic — the ``should_auto_elevate``
gate and the frozen/dev argv split — with monkeypatched platform signals so it runs
on macOS/CI. The setup-command wiring is covered by ``test_setup_cmd_rollback.py``;
this adds the two elevation-specific CLI paths (cancel + auto-elevate short-circuit).
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from cli import elevation, main

# --- build_relaunch_params: frozen exe vs. source/dev run --------------------

def test_build_relaunch_params_frozen_drops_argv0():
    # Frozen exe: sys.argv[0] IS the exe, so the relaunch runs the exe (sys.executable)
    # with only the CLI args after it.
    file, params = elevation.build_relaunch_params(
        [r"C:\Gateway-CLI-Cowork\gateway-cli-cowork.exe", "setup", "--scope", "machine"],
        r"C:\Gateway-CLI-Cowork\gateway-cli-cowork.exe",
        frozen=True,
    )
    assert file == r"C:\Gateway-CLI-Cowork\gateway-cli-cowork.exe"
    assert params == "setup --scope machine"


def test_build_relaunch_params_quotes_paths_with_spaces():
    file, params = elevation.build_relaunch_params(
        ["gw.exe", "setup", "--api-key-helper", r"C:\Program Files\h.exe"],
        "gw.exe",
        frozen=True,
    )
    # list2cmdline must quote the space-bearing path so it survives re-parsing.
    assert '"C:\\Program Files\\h.exe"' in params
    assert params.startswith("setup --api-key-helper ")


def test_build_relaunch_params_dev_keeps_full_argv():
    # Source/dev run: re-run the interpreter with the whole argv (script path included).
    file, params = elevation.build_relaunch_params(
        ["/src/cli/main.py", "setup"], "/usr/bin/python", frozen=False
    )
    assert file == "/usr/bin/python"
    assert params == "/src/cli/main.py setup"


# --- should_auto_elevate: gates -----------------------------------------------

def test_should_auto_elevate_false_off_windows(monkeypatch):
    monkeypatch.setattr(elevation, "is_windows", lambda: False)
    assert elevation.should_auto_elevate() is False


def test_should_auto_elevate_false_when_already_elevated(monkeypatch):
    monkeypatch.setattr(elevation, "is_windows", lambda: True)
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    assert elevation.should_auto_elevate() is False


def test_should_auto_elevate_false_when_not_frozen(monkeypatch):
    monkeypatch.setattr(elevation, "is_windows", lambda: True)
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    monkeypatch.delattr(elevation.sys, "frozen", raising=False)
    monkeypatch.setattr(elevation, "can_consent_elevate", lambda: True)
    assert elevation.should_auto_elevate() is False


def test_should_auto_elevate_false_for_standard_user(monkeypatch):
    # Frozen, non-elevated, but NOT a consent-elevatable admin: a standard user's
    # UAC prompt would run as a different admin and write the wrong HKCU hive.
    monkeypatch.setattr(elevation, "is_windows", lambda: True)
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    monkeypatch.setattr(elevation.sys, "frozen", True, raising=False)
    monkeypatch.setattr(elevation, "can_consent_elevate", lambda: False)
    assert elevation.should_auto_elevate() is False


def test_should_auto_elevate_true_for_consent_admin(monkeypatch):
    monkeypatch.setattr(elevation, "is_windows", lambda: True)
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    monkeypatch.setattr(elevation.sys, "frozen", True, raising=False)
    monkeypatch.setattr(elevation, "can_consent_elevate", lambda: True)
    assert elevation.should_auto_elevate() is True


def test_can_consent_elevate_true_only_for_limited_token(monkeypatch):
    monkeypatch.setattr(elevation, "_elevation_type", lambda: elevation._TOKEN_ELEVATION_TYPE_LIMITED)
    assert elevation.can_consent_elevate() is True
    monkeypatch.setattr(elevation, "_elevation_type", lambda: elevation._TOKEN_ELEVATION_TYPE_FULL)
    assert elevation.can_consent_elevate() is False
    monkeypatch.setattr(elevation, "_elevation_type", lambda: None)
    assert elevation.can_consent_elevate() is False


def test_non_windows_probes_are_inert():
    # These must never touch ctypes.wintypes (unimportable off Windows).
    assert elevation.is_elevated() is False
    assert elevation._elevation_type() is None
    assert elevation.can_consent_elevate() is False


# --- setup_cmd wiring: elevate short-circuits before any real work -----------

def test_setup_relaunches_elevated_and_exits_with_child_code(monkeypatch):
    # When auto-elevation fires, setup must NOT do any CA/config work — it relaunches
    # and exits with the child's code. Any real work would blow up (no stubs here).
    monkeypatch.setattr(main.elevation, "should_auto_elevate", lambda: True)
    captured: dict = {}

    def fake_relaunch(file, params):
        captured["file"] = file
        captured["params"] = params
        return 7

    monkeypatch.setattr(main.elevation, "relaunch_elevated_and_wait", fake_relaunch)

    result = CliRunner().invoke(main.cli, ["setup", "--scope", "machine"])
    assert result.exit_code == 7
    assert "requesting elevation" in result.output.lower()
    # relaunch was invoked (params derive from the live process argv — its exact
    # content is covered by the build_relaunch_params tests, not here).
    assert "file" in captured and "params" in captured


def test_setup_reports_cancelled_elevation(monkeypatch):
    monkeypatch.setattr(main.elevation, "should_auto_elevate", lambda: True)

    def fake_relaunch(file, params):
        raise elevation.ElevationCancelled("declined")

    monkeypatch.setattr(main.elevation, "relaunch_elevated_and_wait", fake_relaunch)

    result = CliRunner().invoke(main.cli, ["setup"])
    assert result.exit_code != 0
    assert "cancelled" in result.output.lower()


def test_setup_no_elevate_flag_skips_gate(monkeypatch):
    # --no-elevate must bypass the gate entirely, even if should_auto_elevate() is True.
    called = {"relaunch": False}

    def relaunch(file, params):
        called["relaunch"] = True
        return 0

    monkeypatch.setattr(main.elevation, "should_auto_elevate", lambda: True)
    monkeypatch.setattr(main.elevation, "relaunch_elevated_and_wait", relaunch)

    # Fail fast right after the gate (in the CA step) so we never touch the real
    # trust store / config writer — the point is only that we got past the gate
    # without relaunching.
    def boom(*a, **k):
        raise click.ClickException("stop after gate")

    monkeypatch.setattr(main, "_install_corporate_ca", boom)

    result = CliRunner().invoke(main.cli, ["setup", "--no-elevate"])
    # We proceeded into setup (hit the CA step) and never relaunched elevated.
    assert called["relaunch"] is False
    assert "stop after gate" in result.output
