"""Shortcuts must render without waiting for a focus-change event."""
import asyncio

import pytest

from installer.app import InstallerApp
from installer.screens.confirm import ConfirmScreen
from installer.screens.footer import InstallerFooter
from installer.screens.logs import LogScreen


def assert_shortcuts(app, expected, *, palette=True):
    footer = app.screen.query_one(InstallerFooter)
    keys = {child.key: child for child in footer.query("FooterKey") if hasattr(child, "key")}
    assert expected | ({"ctrl+p"} if palette else set()) == keys.keys()
    assert footer.region.height == 1
    assert footer.region.bottom == app.size.height
    assert footer.region.y >= 0
    for key in expected:
        assert footer.region.contains_region(keys[key].region), key


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [120, 160])
async def test_shortcuts_on_initial_mount_and_screen_changes(width):
    app = InstallerApp("standalone", dry_run=True)
    probing = asyncio.Event()

    async def refresh():
        await probing.wait()
        return app.states

    class EmptyShell:
        async def capture(self, cmd):
            return 1, ""

    app.refresh_states = refresh
    app.shell = EmptyShell()
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        # Even while the first probe is pending, the dashboard has shortcuts.
        assert_shortcuts(app, {"q", "r", "s", "o"})
        await pilot.press("o")
        await pilot.pause()
        assert_shortcuts(app, {"escape", "ctrl+c", "q", "r", "s", "o"})
        await pilot.press("escape", "s")
        await pilot.pause()
        assert_shortcuts(app, {"escape", "ctrl+s", "q", "r", "s", "o"})
        await pilot.press("escape")
        await pilot.pause()
        assert_shortcuts(app, {"q", "r", "s", "o"})
        await app.push_screen(LogScreen("first_apply"))
        await pilot.pause()
        assert_shortcuts(app, {"escape", "ctrl+c", "q", "r", "s", "o"})
        await pilot.press("escape")
        await app.push_screen(ConfirmScreen("Delete?", "example"))
        await pilot.pause()
        # The modal shows its own cancellation key.
        assert_shortcuts(app, {"escape"}, palette=False)
        await pilot.press("escape")
        probing.set()
        await pilot.pause()
        await pilot.resize_terminal(120, 20)
        await pilot.pause()
        assert_shortcuts(app, {"q", "r", "s", "o"})
