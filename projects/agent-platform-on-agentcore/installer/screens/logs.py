"""Live command output, plus how a failure is presented."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog, Static

from installer.core import diagnose
from installer.core.probe import State
from installer.core.runner import Result
from installer.screens.safetext import set_text

_SECRET_NOTICE = (
    "이 로그에는 민감한 값이 포함될 수 있습니다 (terraform 출력에 tfvars 값이 "
    "섞여 나옵니다). installer/.logs/ 는 gitignore + 0600 입니다."
)


def format_failure(result: Result) -> str:
    """Everything the user needs to act on a failed step."""
    if result.ok:
        return ""

    parts: list[str] = []
    if result.cancelled:
        parts.append(
            "실행을 취소했습니다. terraform apply 중이었다면 state 잠금이 남았을 수 "
            "있습니다 — 다음 실행에서 force-unlock 안내가 나옵니다."
        )
    else:
        parts.append(f"실패 (exit {result.exit_code})")

    output = "\n".join(result.tail)
    if output:
        parts.append("마지막 출력:\n" + output)

    advice = diagnose.explain(output)
    if advice:
        parts.append("해석:\n" + advice)

    if result.log_path is not None:
        parts.append(f"전체 로그: {result.log_path}")
    return "\n\n".join(parts)


class LogScreen(Screen):
    BINDINGS = [
        ("escape", "back", "돌아가기"),
        ("ctrl+c", "cancel", "실행 취소"),
    ]

    def __init__(self, step_id: str) -> None:
        super().__init__()
        self.step_id = step_id

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield Static(_SECRET_NOTICE, id="warning")
            yield RichLog(id="output", highlight=False, markup=False, wrap=True)
            yield Static(id="result")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#output", RichLog)
        step = self.app.plan.by_id(self.step_id)
        log.write(f"$ {step.title}")
        # A worker, not an await in the handler: a handler that awaits a ten
        # minute apply blocks this screen's message loop, so nothing here was
        # painted until the command ended — the terminal kept showing the
        # dashboard frame underneath. Seen on a real first apply.
        self.run_worker(self._run(log), exclusive=True)

    async def _run(self, log: RichLog) -> None:
        result = await self.app.run_step(self.step_id, on_line=log.write)

        if result.ok:
            set_text(self.query_one("#result", Static), "완료. 상태를 다시 점검합니다…")
            await self.app.refresh_states()
            set_text(self.query_one("#result", Static), self._verdict())
        else:
            set_text(self.query_one("#result", Static), format_failure(result))

    def _verdict(self) -> str:
        """What to tell the user after a command exited 0.

        Exit 0 is not proof: a partial apply exits clean with empty outputs, and
        a probe that hit a throttle or timeout answers UNKNOWN. Saying
        "완료 — 프로브 결과: unknown" in that case contradicts itself, so the three
        outcomes get three distinct messages.
        """
        state = self.app.states[self.step_id]
        detail = self.app.details.get(self.step_id, "")

        if state is State.DONE:
            return f"완료 — 상태 확인됨. {detail}".strip()
        if state is State.UNKNOWN:
            return (
                "명령은 성공했지만 상태를 확인할 수 없습니다 — 직접 확인이 필요합니다.\n"
                f"{detail}".strip()
            )
        return (
            "명령은 성공했지만 아직 완료로 보이지 않습니다. 로그를 확인하고 "
            f"다시 실행하십시오.\n{detail}".strip()
        )

    async def action_cancel(self) -> None:
        await self.app.cancel_current()

    def action_back(self) -> None:
        self.app.pop_screen()
