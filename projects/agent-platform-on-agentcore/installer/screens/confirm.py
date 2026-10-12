"""Typed confirmation for operations that cannot be undone cheaply."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from installer.screens.safetext import set_text
from textual.widgets import Button, Input, Label, Static


def matches(typed: str, expected: str) -> bool:
    """Exact match after trimming. Case matters: these are identifiers."""
    return typed.strip() == expected and bool(expected)


class ConfirmScreen(ModalScreen[bool]):
    # Esc closes the dialog as "no"; before this the only way out was the 취소
    # button (Tab, Tab, Enter) or the mouse.
    BINDINGS = [("escape", "dismiss(False)", "취소")]

    def __init__(self, prompt: str, expected: str, extra: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.expected = expected
        self.extra = extra

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.prompt)
            if self.extra:
                yield Static(self.extra)
            yield Label(f"계속하려면 환경 이름을 입력하십시오: {self.expected}")
            yield Input(id="typed", placeholder=self.expected)
            yield Static("", id="confirm-error")
            yield Button("실행", id="go", variant="error")
            yield Button("취소", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(False)
            return
        typed = self.query_one("#typed", Input).value
        if matches(typed, self.expected):
            self.dismiss(True)
        else:
            set_text(self.query_one("#confirm-error", Static),
                     f"'{self.expected}' 와 정확히 일치해야 합니다.")

    def on_input_submitted(self, _event) -> None:
        self.on_button_pressed(Button.Pressed(self.query_one("#go", Button)))
