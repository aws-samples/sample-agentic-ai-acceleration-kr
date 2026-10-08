"""Settings: Required / Advanced / Derived."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static, TabbedContent, TabPane

from installer.core import values
from installer.screens.safetext import set_text


class Settings(Screen):
    BINDINGS = [("escape", "back", "돌아가기"), ("ctrl+s", "save", "저장")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        self._inputs: dict[str, Input] = {}
        fields = values.load_fields(self.app.env)
        # Captured once: the event handlers validate against these rather than
        # re-reading variables.tf per keystroke.
        self._variables = {f.variable.name: f.variable for f in fields}

        with TabbedContent():
            with TabPane("Required", id="required"):
                yield from self._pane(fields, "required")
            with TabPane("Advanced", id="advanced"):
                yield from self._pane(fields, "advanced")
            with TabPane("Derived (읽기 전용)", id="derived"):
                yield VerticalScroll(Static(id="derived-body"))
        yield Static(id="save-status")
        yield Button("저장 (Ctrl+S)", id="save", variant="primary")
        yield Footer()

    def _pane(self, fields, tab: str):
        with VerticalScroll():
            for field in (f for f in fields if f.tab == tab):
                variable = field.variable
                yield Label(f"{variable.name}{' *' if not variable.has_default else ''}")
                if variable.description:
                    yield Static(variable.description.splitlines()[0], classes="help")
                widget = Input(
                    value=field.value,
                    password=variable.sensitive,
                    id=f"in-{variable.name}",
                )
                self._inputs[variable.name] = widget
                yield widget
                yield Static("", id=f"err-{variable.name}")

    async def on_mount(self) -> None:
        derived = await values.derived(self.app.env, self.app.shell)
        body = (
            "\n".join(f"{k:28} {v}" for k, v in derived.items())
            if derived
            else "아직 terraform output 이 없습니다. apply 후에 채워집니다."
        )
        set_text(
            self.query_one("#derived-body", Static),
            "이 값들은 Terraform 이 소유합니다. 여기서 고쳐도 다음 apply 에서 "
            "되돌아가므로 읽기 전용입니다.\n\n" + body,
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        name = event.input.id.removeprefix("in-")
        # Validate against the variables captured when the form was built. Re-reading
        # variables.tf and tfvars on every keystroke re-parses two files per
        # character, and worse, could validate different characters of one edit
        # against different definitions if the files changed underneath.
        variable = self._variables.get(name)
        if variable is None:
            return
        message = values.validate(variable, event.value)
        set_text(self.query_one(f"#err-{name}", Static), message or "")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.run_worker(self.action_save())

    async def action_save(self) -> None:
        fields = self._variables
        edits = {name: widget.value for name, widget in self._inputs.items()}

        blocking = [
            f"{name}: {message}"
            for name, raw in edits.items()
            if (message := values.validate(fields[name], raw))
            and not fields[name].has_default
        ]
        if blocking:
            set_text(self.query_one("#save-status", Static),
                     "저장하지 않았습니다:\n" + "\n".join(blocking))
            return

        from installer.core.files import WriteRefused

        try:
            result = values.save(self.app.env, edits)
        except WriteRefused as exc:
            set_text(self.query_one("#save-status", Static), f"거부됨: {exc}")
            return

        note = f"저장했습니다: {result.path}"
        if result.warning:
            note += f"\n경고: {result.warning}"
        set_text(self.query_one("#save-status", Static), note)
        # Steps bake the resolved project into their commands, so a saved
        # `project` change leaves the existing plan pointing at the old prefix.
        self.app.rebuild_plan()
        await self.app.refresh_states()

    def action_back(self) -> None:
        self.app.pop_screen()
