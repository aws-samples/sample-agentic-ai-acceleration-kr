"""Step list with live status."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from installer.core.probe import State
from installer.core.steps import Command, FileWrite
from installer.screens.safetext import set_text

_LABEL = {
    State.DONE: ("완료", "state-done"),
    State.PENDING: ("대기", "state-pending"),
    State.UNKNOWN: ("판정불가", "state-unknown"),
}


class Dashboard(Screen):
    BINDINGS = [("enter", "run_selected", "선택한 단계 실행")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield DataTable(id="steps", cursor_type="row")
            yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        # Paint the step list at once and probe in a worker. Awaiting the probes
        # here left the terminal blank for as long as `terraform plan` takes
        # (tens of seconds once the stack exists) — and nothing, footer
        # included, was drawn until they finished.
        self._probing = True
        self._fill()
        self.run_worker(self._probe_then_fill(), exclusive=True)

    async def _probe_then_fill(self) -> None:
        try:
            await self.app.refresh_states()
        finally:
            self._probing = False
        self._fill()

    def on_screen_resume(self) -> None:
        # Settings saves and finished steps re-probe while this screen is
        # suspended; without this the table kept showing the rows from mount.
        self._fill()

    def refill(self) -> None:
        """Redraw from app.states. The app calls this after `r` re-probes."""
        self._fill()

    def _fill(self) -> None:
        table = self.query_one("#steps", DataTable)
        # Columns are added here, not in on_mount, so a table that was cleared
        # or recomposed can never receive a 4-cell row with 0 columns — that
        # ValueError took the whole app down on a real `r` press.
        if not table.columns:
            table.add_columns("#", "단계", "상태", "비고")
        cursor = table.cursor_row
        table.clear()
        for index, step in enumerate(self.app.plan.steps):
            state = self.app.states[step.id]
            label, _ = _LABEL[state]
            blocked = self.app.plan.blocked_by(step.id, self.app.states)
            note = self.app.details.get(step.id, "")
            if getattr(self, "_probing", False):
                label, note = "확인 중", "상태를 점검하고 있습니다…"
            elif blocked and state is not State.DONE:
                note = f"선행 필요: {', '.join(blocked)}"
            table.add_row(str(index), step.title, label, note[:60], key=step.id)
        if 0 <= cursor < table.row_count:
            table.move_cursor(row=cursor)
        self._show_detail()

    def _show_detail(self) -> None:
        table = self.query_one("#steps", DataTable)
        if not 0 <= table.cursor_row < len(self.app.plan.steps):
            return
        step = self.app.plan.steps[table.cursor_row]
        action = step.action
        if isinstance(action, Command):
            what = f"실행: {action.display()}"
        elif isinstance(action, FileWrite):
            what = f"파일 쓰기: {action.description}"
        else:
            what = "점검만 수행합니다 (실행할 명령 없음)"
        detail = self.app.details.get(step.id, "")
        set_text(self.query_one("#detail", Static), f"{what}\n\n{detail}")

    def on_data_table_row_highlighted(self, _event) -> None:
        self._show_detail()

    async def on_data_table_row_selected(self, _event) -> None:
        # The table has focus and binds Enter itself (select_cursor), so the
        # screen-level "enter" binding above never fires while a row is focused.
        # Textual reports that press as RowSelected; treat it as "run".
        await self.action_run_selected()

    async def action_run_selected(self) -> None:
        table = self.query_one("#steps", DataTable)
        # The plan can be rebuilt while this screen is suspended (a settings save
        # does it). Today's rebuild cannot change the step count, but indexing a
        # widget's cursor into a separately-owned list should not depend on that.
        if not 0 <= table.cursor_row < len(self.app.plan.steps):
            await self.app.refresh_states()
            self._fill()
            return
        step = self.app.plan.steps[table.cursor_row]

        blocked = self.app.plan.blocked_by(step.id, self.app.states)
        if blocked:
            self.notify(f"선행 단계가 끝나지 않았습니다: {', '.join(blocked)}",
                        severity="warning")
            return

        if isinstance(step.action, FileWrite) and step.action.target == "tfvars":
            self.notify("설정 화면(s)에서 값을 입력하고 저장하십시오.",
                        severity="information")
            self.app.push_screen("settings")
            return
        if step.action is None:
            await self.app.refresh_states()
            self._fill()
            return

        from installer.screens.logs import LogScreen

        await self.app.push_screen(LogScreen(step_id=step.id))
