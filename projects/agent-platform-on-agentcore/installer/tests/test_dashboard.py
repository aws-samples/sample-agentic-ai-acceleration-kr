"""
Dashboard redraw paths, driven through Textual's headless pilot.

Two real-terminal crashes motivate this file: `r` used to recompose the screen,
which rebuilt the step table without columns and made the next redraw raise;
and returning from Settings/Log screens left the table showing the rows from
mount time. Probes are stubbed so nothing here talks to AWS.
"""
import os
import sys

import pytest
from textual.widgets import DataTable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.app import InstallerApp  # noqa: E402
from installer.core.probe import State  # noqa: E402


def _app(states: dict[str, State]) -> InstallerApp:
    app = InstallerApp(env="standalone", dry_run=True)

    async def fake_refresh():
        for step in app.plan.steps:
            app.states[step.id] = states.get(step.id, State.PENDING)
            app.details[step.id] = ""
        return app.states

    app.refresh_states = fake_refresh  # type: ignore[method-assign]
    return app


def _table(app: InstallerApp) -> DataTable:
    return app.screen.query_one("#steps", DataTable)


@pytest.mark.asyncio
async def test_refresh_key_redraws_without_losing_the_table():
    app = _app({"preflight": State.DONE})
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert _table(app).row_count == len(app.plan.steps)

        await pilot.press("down", "down", "r")
        await pilot.pause()

        table = _table(app)
        assert table.row_count == len(app.plan.steps)
        assert len(table.columns) == 4
        assert table.cursor_row == 2, "r must not reset the selection"

        # The crash surfaced on the key press after `r`; Enter on a probe-only
        # step takes the refill path that raised.
        await pilot.press("up", "up", "enter")
        await pilot.pause()
        assert _table(app).row_count == len(app.plan.steps)


@pytest.mark.asyncio
async def test_dashboard_redraws_when_it_resumes():
    states = {"preflight": State.PENDING}
    app = _app(states)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert "대기" in str(_table(app).get_row_at(0)[2])

        states["preflight"] = State.DONE
        await app.refresh_states()          # what a Settings save does
        app.push_screen("settings")
        await pilot.pause()
        app.pop_screen()
        await pilot.pause()

        assert "완료" in str(_table(app).get_row_at(0)[2])


@pytest.mark.asyncio
async def test_enter_on_a_focused_row_runs_the_step():
    """The DataTable owns Enter when focused; RowSelected must still run the step.

    Shipped behaviour under Textual 8.2.8 was that Enter did nothing on either
    table screen, because the widget's own binding shadowed the screen's.
    """
    from installer.screens.logs import LogScreen

    app = _app({"preflight": State.DONE})
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.focused, DataTable)
        await pilot.press("down", "enter")       # row 1: bootstrap (a Command)
        await pilot.pause()
        assert isinstance(app.screen, LogScreen)
        assert app.screen.step_id == "bootstrap"


@pytest.mark.asyncio
async def test_enter_on_operations_runs_the_selected_operation():
    from textual.widgets import RichLog

    app = _app({})
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        await pilot.press("down", "down", "enter")   # logs tail: non-destructive
        await pilot.pause()
        lines = app.screen.query_one("#ops-output", RichLog).lines
        assert lines, "dry-run must echo the assembled command"


@pytest.mark.asyncio
async def test_log_screen_paints_output_while_the_step_is_still_running():
    """The apply takes minutes; the user must see it progressing, not a frozen
    dashboard. Guards against awaiting run_step inside on_mount."""
    import asyncio
    import re

    from installer.core.runner import Result

    class SlowRunner:
        async def run(self, cmd, step_id, on_line):
            for i in range(6):
                on_line(f"progress-{i}")
                await asyncio.sleep(0.3)
            return Result(exit_code=0, cancelled=False, log_path=None, tail=())

        async def cancel(self):
            pass

    app = _app({"preflight": State.DONE})
    app.dry_run = False
    app.runner = SlowRunner()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("down", "enter")        # bootstrap, a Command step
        await asyncio.sleep(0.8)
        frame = re.sub(r"<[^>]+>", "", app.export_screenshot())
        assert "progress-0" in frame, "log output must appear before the step ends"
        assert "사전 점검" not in frame, "the dashboard frame must be gone"
        await asyncio.sleep(1.5)
        frame = re.sub(r"<[^>]+>", "", app.export_screenshot())
        assert "progress-5" in frame
        assert "완료" in frame


@pytest.mark.asyncio
async def test_dashboard_paints_before_the_probes_finish():
    """Probes can take tens of seconds (terraform plan); the table must be
    visible immediately with a placeholder, then fill in."""
    import asyncio
    import re

    app = InstallerApp(env="standalone", dry_run=True)
    gate = asyncio.Event()

    async def slow_refresh():
        await gate.wait()
        for step in app.plan.steps:
            app.states[step.id] = State.DONE
            app.details[step.id] = ""
        return app.states

    app.refresh_states = slow_refresh  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.3)
        table = _table(app)
        assert table.row_count == len(app.plan.steps)
        assert str(table.get_row_at(0)[2]) == "확인 중"
        # Rich's SVG export writes spaces as entities, so look for a word.
        frame = re.sub(r"<[^>]+>", "", app.export_screenshot())
        assert "사전" in frame, "the table must be painted before probes finish"
        gate.set()
        await pilot.pause()
        await asyncio.sleep(0.2)
        assert str(_table(app).get_row_at(0)[2]) == "완료"


@pytest.mark.asyncio
async def test_ctrl_c_on_operations_cancels_the_running_command():
    """`aws logs tail --follow` never ends on its own; the screen must offer a
    way out, and `q` must cancel rather than quit while it runs."""
    import asyncio

    from installer.core.runner import Result

    class HangingRunner:
        def __init__(self):
            self.cancelled = False
            self._ev = asyncio.Event()

        async def run(self, cmd, step_id, on_line):
            on_line("tailing…")
            await self._ev.wait()
            return Result(exit_code=130, cancelled=True, log_path=None, tail=())

        async def cancel(self):
            self.cancelled = True
            self._ev.set()

    app = _app({})
    app.dry_run = False
    runner = HangingRunner()
    app.runner = runner
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        await pilot.press("down", "down", "enter")   # 서버 로그 tail
        await asyncio.sleep(0.3)
        assert app.is_running, "an operation must mark the app busy"
        await pilot.press("ctrl+c")
        await asyncio.sleep(0.3)
        assert runner.cancelled
        assert app.is_running is False
        assert app._running, "ctrl+c must not quit the app"


@pytest.mark.asyncio
async def test_escape_closes_the_destroy_confirmation_as_cancel():
    from installer.screens.confirm import ConfirmScreen

    class PlanShell:
        async def capture(self, cmd):
            return 0, "Plan: 0 to add, 0 to change, 7 to destroy."

    app = _app({})
    app.shell = PlanShell()                      # no real terraform plan -destroy
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        await pilot.press("down", "down", "down", "down", "enter")   # destroy
        for _ in range(20):
            await pilot.pause(0.1)
            if isinstance(app.screen, ConfirmScreen):
                break
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert "취소" in str(app.screen.query_one("#ops-result").render())
