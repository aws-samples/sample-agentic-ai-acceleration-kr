"""Day-two operations: redeploy, rebuild, logs, park, destroy."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from installer.core import steps as stepsmod
from installer.core.env import resolve_project
from installer.core.runner import Command
from installer.core.steps import FileWrite
from installer.screens.confirm import ConfirmScreen
from installer.screens.logs import format_failure
from installer.screens.safetext import set_text


def destroy_protection_note(env) -> str:
    """Warn before a destroy that DynamoDB deletion protection will reject.

    The tables ship with deletion protection unless `allow_destroy = true` was
    applied first; a real destroy ran for eight minutes and then failed on the
    tables with everything around them already gone.
    """
    from installer.core import hcl

    path = env.tfvars_path
    values = hcl.read_values(path.read_text()) if path.is_file() else {}
    if values.get("allow_destroy", "").strip() == "true":
        return ""
    return (
        "\n\n주의: allow_destroy 가 true 가 아닙니다. DynamoDB 삭제 보호 때문에 "
        "destroy 가 테이블에서 실패합니다. 먼저 설정(s) Advanced 탭에서 allow_destroy 를 "
        "true 로 저장하고 2차 apply 를 한 번 실행한 뒤 destroy 하십시오."
    )


class Operations(Screen):
    BINDINGS = [
        ("escape", "back", "돌아가기"),
        ("enter", "run_selected", "실행"),
        # Without this, Ctrl+C here fell through to Textual's own handler and
        # showed "Press ctrl+q to quit" while `aws logs tail --follow` kept
        # running — there was no way to stop it from the screen.
        ("ctrl+c", "cancel", "실행 취소"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield DataTable(id="ops", cursor_type="row")
            yield RichLog(id="ops-output", highlight=False, markup=False, wrap=True)
            yield Static(id="ops-result")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#ops", DataTable)
        table.add_columns("작업", "비고")
        self._ops = stepsmod.operations(self.app.env)
        for op in self._ops:
            note = "확인 필요 (환경 이름 입력)" if op.destructive else ""
            table.add_row(op.title, note, key=op.id)

    def action_run_selected(self) -> None:
        """Run the selected operation in a worker.

        `push_screen_wait` (used for the destroy/park confirmation) raises
        NoActiveWorker unless it is awaited inside one, and a key binding is not a
        worker — so calling the async handler directly crashed every destructive
        operation. Verified on a real run before this wrapper existed.
        """
        self.run_worker(self._run_selected(), exclusive=True)

    def on_data_table_row_selected(self, _event) -> None:
        # Same shadowing as the dashboard: the focused DataTable consumes Enter
        # and emits RowSelected instead of letting the screen binding fire.
        self.action_run_selected()

    async def _run_selected(self) -> None:
        if self.app.is_running:
            set_text(self.query_one("#ops-result", Static),
                     f"다른 단계가 실행 중입니다: {self.app._running_step}")
            return

        table = self.query_one("#ops", DataTable)
        op = self._ops[table.cursor_row]
        log = self.query_one("#ops-output", RichLog)
        # The previous operation's verdict ("완료했습니다.") stayed on screen
        # under a running destroy; clear it so the footer line reflects this run.
        set_text(self.query_one("#ops-result", Static), "")

        if op.destructive:
            extra = ""
            if op.id == "destroy":
                # Always show the blast radius, dry-run included: `plan -destroy`
                # is read-only, and the preview is the whole point of asking.
                plan = Command(
                    argv=("terraform", f"-chdir={self.app.env.tf_dir}",
                          "plan", "-destroy", "-no-color")
                )
                code, out = await self.app.shell.capture(plan)
                summary = [line for line in out.splitlines() if "Plan:" in line]
                extra = summary[0] if summary else "삭제 계획을 읽을 수 없었습니다."
                extra += destroy_protection_note(self.app.env)
            confirmed = await self.app.push_screen_wait(
                ConfirmScreen(
                    prompt=f"'{op.title}' 를 실행합니다.",
                    # The resolved prefix, not the env default: operations target
                    # what tfvars says, so that is the name being confirmed.
                    expected=resolve_project(self.app.env)[0],
                    extra=extra,
                )
            )
            if not confirmed:
                set_text(self.query_one("#ops-result", Static), "취소했습니다.")
                return

        if isinstance(op.action, FileWrite):
            set_text(self.query_one("#ops-result", Static),
                     "대시보드의 '이미지 빌드 & ECR push' 단계를 쓰십시오.")
            return

        if self.app.dry_run:
            log.write(f"[dry-run] {op.action.display()}")
            return

        # Mark the app busy for the duration so `q` cancels instead of quitting
        # (leaving the tail/apply process behind), same as dashboard steps.
        self.app._running_step = op.id
        try:
            result = await self.app.runner.run(op.action, op.id, log.write)
        finally:
            if self.app._running_step == op.id:
                self.app._running_step = None
        set_text(self.query_one("#ops-result", Static),
                 format_failure(result) or "완료했습니다.")
        await self.app.refresh_states()

    async def action_cancel(self) -> None:
        await self.app.cancel_current()

    def action_back(self) -> None:
        self.app.pop_screen()
