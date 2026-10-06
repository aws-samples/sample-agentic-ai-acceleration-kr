"""
Tests for rendering untrusted text into Textual widgets.

Everything the installer shows in a Static is terraform / docker / aws output,
which contains square brackets and ANSI escapes. Static.update(str) parses its
argument as console markup, so an unbalanced `[` raises MarkupError and takes the
screen down mid-deploy — precisely when the user needs to read the message.

The string in test_the_string_that_actually_crashed is verbatim from a real run:
`terraform init` against a non-existent bucket, which crashed the log screen.
"""
import os
import sys

import pytest
from textual.app import App
from textual.widgets import Static

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.screens.safetext import plain, set_text  # noqa: E402

CRASHER = 'bucket="ap\x1b[0m-standalone-tfstate-123456789012]\x1b[0m\n'


class _Harness(App):
    def compose(self):
        yield Static(id="target")


@pytest.mark.parametrize("text", [
    CRASHER,
    "Error: unbalanced [ bracket",
    "Plan: 42 to add, 0 to change, 1 to destroy.",
    "[bold]not a tag[/bold]",
    "",
])
def test_untrusted_text_never_raises(text):
    async def run():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            set_text(app.query_one("#target", Static), text)
            await pilot.pause()
    import asyncio
    asyncio.run(run())


def test_set_text_passes_a_content_not_a_markup_string():
    """The mechanism, since the crash only surfaces on a later render pass.

    Static.update(str) stores the string and runs it through markup parsing;
    update(Content) takes it literally. Asserting the type is what pins the fix —
    a future edit that drops the Content wrapper reintroduces the crash.
    """
    from textual.content import Content

    async def run():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            widget = app.query_one("#target", Static)
            set_text(widget, CRASHER)
            await pilot.pause()
            stored = widget._Static__content
            assert isinstance(stored, Content), f"got {type(stored).__name__}"
    import asyncio
    asyncio.run(run())


def test_ansi_escapes_are_stripped():
    assert plain("\x1b[0mhello\x1b[1;32m world") == "hello world"
    assert plain("no escapes") == "no escapes"
