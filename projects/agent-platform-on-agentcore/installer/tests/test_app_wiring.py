"""
Tests for app wiring, without a terminal.

Only the seam between the app and core is exercised here — no widget rendering.
Textual snapshot tests break on layout churn and catch few real bugs, so the UI
stays a thin renderer over core data and the logic is tested where it lives.

The import guard is the one structural rule worth a test: nothing under
installer/core may import textual. That is what lets every earlier task's tests
run headless, and it is easy to violate by reflex when adding a UI convenience
to a core module.
"""
import asyncio
import os
import pathlib
import subprocess
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

CORE = pathlib.Path(__file__).resolve().parents[1] / "core"


def run(coro):
    return asyncio.run(coro)


def test_core_never_imports_textual():
    hits = subprocess.run(
        ["grep", "-rn", "textual", str(CORE)], capture_output=True, text=True
    )
    assert hits.stdout == "", f"core must stay UI-free:\n{hits.stdout}"


def test_app_module_exposes_the_expected_surface():
    from installer.app import InstallerApp

    for name in ("refresh_states", "run_step", "cancel_current"):
        assert hasattr(InstallerApp, name), name


def test_app_builds_a_plan_for_its_environment():
    from installer.app import InstallerApp

    app = InstallerApp(env="standalone", dry_run=False)
    assert app.env.name == "standalone"
    assert [s.id for s in app.plan.steps][0] == "preflight"


def test_dry_run_is_carried_into_the_app():
    from installer.app import InstallerApp

    assert InstallerApp(env="standalone", dry_run=True).dry_run is True


def test_log_dir_is_inside_the_installer_and_gitignored():
    from installer.app import InstallerApp

    app = InstallerApp(env="standalone", dry_run=False)
    assert app.log_dir.name == ".logs"
    assert app.log_dir.parent.name == "installer"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(app.log_dir / "x.log")],
        cwd=app.log_dir.parents[1],
    )
    assert ignored.returncode == 0, "log dir must be gitignored: logs carry secrets"


def test_shell_capture_handles_concurrent_commands():
    """Concurrent probe capture must not raise or clobber results.

    This tests the fix for a critical race condition where concurrent probes
    would share a single Runner's _proc field, causing 'NoneType' has no
    attribute 'wait' errors. Each probe should spawn and await its own subprocess.
    """
    from installer.app import RunnerShell
    from installer.core.runner import Command

    async def check():
        shell = RunnerShell()

        # Run 5 commands concurrently, each with distinguishable output.
        # If interleaving or clobbering occurs, outputs will be wrong or missing.
        commands = [
            Command(argv=("sh", "-c", f"echo probe-{i}"))
            for i in range(5)
        ]
        results = await asyncio.gather(*(shell.capture(cmd) for cmd in commands))

        # All should succeed with no exceptions.
        assert len(results) == 5
        for i, (exit_code, output) in enumerate(results):
            assert isinstance(exit_code, int), f"result {i}: exit_code not int"
            assert isinstance(output, str), f"result {i}: output not str"
            assert exit_code == 0, f"result {i}: exit_code {exit_code}"
            assert f"probe-{i}" in output, f"result {i}: expected 'probe-{i}' in '{output}'"

    run(check())


def test_run_step_refuses_overlapping_calls():
    """Overlapping run_step calls must refuse the second, not corrupt state.

    The guard checks is_running at the start and refuses with a non-ok Result.
    We test this by manually simulating an in-flight call.
    """
    from installer.app import InstallerApp

    async def check():
        app = InstallerApp(env="standalone", dry_run=True)

        # Simulate first call in flight by setting the flag manually.
        app._running_step = "preflight"

        # Now try to call run_step while first is supposedly running.
        result = await app.run_step("bootstrap", lambda _: None)

        # Second call should be rejected.
        assert not result.ok, f"Second call should be rejected: {result}"
        assert any("실행 중" in line for line in result.tail), \
            f"Result tail should mention running step: {result.tail}"

        # Flag should still be set to "preflight", not changed to "bootstrap".
        assert app._running_step == "preflight", \
            f"Flag should not change on rejected call: {app._running_step}"

        # Clean up the flag and verify it can still be set and cleared.
        app._running_step = None
        assert app.is_running is False

    run(check())


def test_run_step_sequential_calls_both_succeed():
    """Sequential run_step calls should both succeed (no guard blocking).

    This guards against over-zealous guards that break normal usage where
    steps run one after another.
    """
    from installer.app import InstallerApp

    async def check():
        app = InstallerApp(env="standalone", dry_run=True)
        lines1, lines2 = [], []

        # First call, await it to completion.
        r1 = await app.run_step("preflight", lines1.append)
        assert r1.ok, f"First call should succeed; got {r1}"

        # Second call after the first is done.
        r2 = await app.run_step("bootstrap", lines2.append)
        assert r2.ok, f"Second call should succeed; got {r2}"

        # Flag should be clean.
        assert app.is_running is False
        assert app._running_step is None

    run(check())


# ── commands must reflect the filesystem at run time, not at startup ─────────
#
# Steps bake their argv when the plan is built. `init` only carries
# -backend-config once backend.hcl exists, and that file is created by an EARLIER
# step in the same session — so a plan built at startup produced an init command
# without the flag, and terraform died with 'Missing Required Value: The
# attribute "bucket" is required by the backend'. Found on a real from-zero run
# AFTER the _init_command fix, because the fix was correct but the plan was stale.

def test_run_step_rebuilds_the_plan_before_reading_the_action(tmp_path, monkeypatch):
    import asyncio

    from installer.app import InstallerApp

    app = InstallerApp(env="standalone", dry_run=True)

    calls = {"n": 0}
    original = app.rebuild_plan

    def counting_rebuild():
        calls["n"] += 1
        original()

    monkeypatch.setattr(app, "rebuild_plan", counting_rebuild)
    asyncio.run(app.run_step("init", lambda _line: None))
    assert calls["n"] >= 1, "run_step must rebuild the plan before dispatching"


def test_init_gains_backend_config_once_backend_hcl_appears(tmp_path, monkeypatch):
    """The concrete sequence that failed: backend step writes the file, then init."""
    from dataclasses import replace

    from installer.core import env as envmod
    from installer.core import steps as stepsmod

    env = replace(envmod.get("standalone"), name="seq-test")
    monkeypatch.setattr(type(env), "tf_dir", property(lambda self: tmp_path))

    before = stepsmod.build_plan(env).by_id("init").action.argv
    assert not any(a.startswith("-backend-config=") for a in before)

    (tmp_path / "backend.hcl").write_text('bucket = "b"\n')     # the backend step

    after = stepsmod.build_plan(env).by_id("init").action.argv
    assert any(a.startswith("-backend-config=") for a in after)
