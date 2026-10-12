"""Rendering untrusted text into Textual widgets.

Everything this installer displays in a Static is terraform, docker or aws
output, and those contain square brackets and ANSI escapes. Textual parses
`Static.update(str)` as console markup, so an unbalanced `[` raises MarkupError
and takes the screen down mid-deploy — which is exactly when the user needs to
read the message.

Verified on a real run: `terraform init` against a bucket that does not exist
produced

    ...bucket="ap<ESC>[0m-standalone-tfstate-123456789012]<ESC>[0m

and the resulting MarkupError crashed the log screen instead of showing the
failure.
"""
from __future__ import annotations

import re

from textual.content import Content
from textual.widgets import Static

# Terraform colours its output even with -no-color on some paths, and docker
# always does. Stripped rather than rendered: this text lands in a plain Static.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(text: str) -> str:
    """Strip ANSI escapes so the text renders as written."""
    return _ANSI.sub("", text)


def set_text(widget: Static, text: str) -> None:
    """Update a Static with text that may contain brackets or escapes.

    Passing a `Content` is the load-bearing part: `Static.update(str)` runs the
    string through markup parsing (Static's `markup` flag is constructor-only, so
    it cannot be turned off per call), while a Content is taken literally.
    """
    widget.update(Content(plain(text)))
