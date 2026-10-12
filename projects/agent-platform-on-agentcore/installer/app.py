"""Textual application shell.

Holds the plan, the runner and the current state map; screens render them. All
decision logic lives in installer.core so it stays testable without a terminal.
"""
from __future__ import annotations

import asyncio
import os

from textual.app import App
from textual.binding import Binding

from installer.core import env as envmod
from installer.core import steps as stepsmod
from installer.core.probe import ProbeOutcome, State
from installer.core.runner import Command, Result, Runner
from installer.core.steps import FileWrite


class RunnerShell:
    """Adapts Command execution to the read-only Shell protocol the probes expect.

    Probes are read-only and concurrent, so they spawn their own subprocesses
    rather than routing through the shared Runner (which is designed for
    sequential step execution with cancellation).
    """

    async def capture(self, cmd: Command) -> tuple[int, str]:
        environ = {**os.environ, **(cmd.env or {})}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd.argv,
                cwd=str(cmd.cwd) if cmd.cwd else None,
                env=environ,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            message = f"실행할 수 없습니다: {cmd.argv[0]} ({exc})"
            return 127, message

        output, _ = await proc.communicate()
        exit_code = proc.returncode or 0
        text = output.decode(errors="replace").rstrip("\n")
        return exit_code, text


class InstallerApp(App):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("q", "quit_guarded", "종료"),
        # Textual's default ctrl+q quits unconditionally; route it through the
        # same guard so a running apply is cancelled, not abandoned mid-flight.
        Binding("ctrl+q", "quit_guarded", "종료", show=False, priority=True),
        ("r", "refresh", "상태 다시 점검"),
        ("s", "push_screen('settings')", "설정"),
        ("o", "push_screen('operations')", "운영"),
    ]
    SCREENS = {}

    def __init__(self, env: str, dry_run: bool) -> None:
        super().__init__()
        self.env = envmod.get(env)
        self.dry_run = dry_run
        self.plan = stepsmod.build_plan(self.env)
        self.log_dir = envmod.REPO_ROOT / "installer" / ".logs"
        self.runner = Runner(log_dir=self.log_dir)
        # Probes spawn their own subprocesses to avoid queueing behind step execution.
        self.shell: object = RunnerShell()
        self.states: dict[str, State] = {s.id: State.UNKNOWN for s in self.plan.steps}
        self.details: dict[str, str] = {}
        self._running_step: str | None = None

    def on_mount(self) -> None:
        from installer.screens.dashboard import Dashboard
        from installer.screens.operations import Operations
        from installer.screens.settings import Settings

        self.install_screen(Settings(), name="settings")
        self.install_screen(Operations(), name="operations")
        self.push_screen(Dashboard())

    def rebuild_plan(self) -> None:
        """Re-derive the step list after settings change.

        Commands embed the resolved project (from tfvars), so editing it leaves
        the built plan targeting the previous prefix. Existing statuses are kept
        for steps that survive the rebuild; the caller re-probes anyway.
        """
        self.plan = stepsmod.build_plan(self.env)
        for step in self.plan.steps:
            self.states.setdefault(step.id, State.UNKNOWN)

    async def refresh_states(self) -> dict[str, State]:
        """Re-derive every step's status. No progress file is consulted."""
        async def one(step) -> tuple[str, ProbeOutcome]:
            try:
                return step.id, await step.probe(self.env, self.shell)
            except Exception as exc:  # a probe must never take the app down
                return step.id, ProbeOutcome(State.UNKNOWN, f"프로브 실패: {exc}")

        results = await asyncio.gather(*(one(s) for s in self.plan.steps))
        for step_id, outcome in results:
            self.states[step_id] = outcome.state
            self.details[step_id] = outcome.detail
        return self.states

    async def run_step(self, step_id: str, on_line=None) -> Result:
        # Refuse overlapping executions: single-flight guard.
        if self.is_running:
            message = f"다른 단계가 실행 중입니다: {self._running_step}"
            sink = on_line or (lambda _line: None)
            sink(message)
            return Result(exit_code=1, cancelled=False, log_path=None, tail=(message,))

        self._running_step = step_id
        sink = on_line or (lambda _line: None)
        try:
            # Steps bake their commands at build time from files earlier steps
            # create: `init` needs -backend-config only once backend.hcl exists,
            # and the plan was built before the backend step ran. Rebuilding here
            # means a command always reflects the filesystem as it is now.
            self.rebuild_plan()
            action = self.plan.by_id(step_id).action

            if isinstance(action, Command):
                if self.dry_run:
                    sink(f"[dry-run] {action.display()}")
                    return Result(exit_code=0, cancelled=False, log_path=None, tail=())
                return await self.runner.run(action, step_id, sink)

            if isinstance(action, FileWrite):
                return await self._run_file_write(step_id, action, sink)

            return Result(exit_code=0, cancelled=False, log_path=None, tail=())
        finally:
            # Only clear the flag if this call set it (it's still step_id, not changed).
            if self._running_step == step_id:
                self._running_step = None

    async def _run_file_write(self, step_id: str, action, sink) -> Result:
        from installer.core import writes

        if action.target == "tfvars":
            sink("설정 화면(s)에서 값을 입력하고 저장하십시오.")
            return Result(exit_code=1, cancelled=False, log_path=None,
                          tail=("tfvars 는 설정 화면에서 저장합니다.",))

        if self.dry_run:
            sink(f"[dry-run] {action.description}")
            return Result(exit_code=0, cancelled=False, log_path=None, tail=())

        try:
            if action.target == "backend":
                account = await writes.account_id(self.shell, self.env)
                result = await writes.write_backend(self.env, self.shell, account)
                sink(f"작성: {result.path}")
                if result.warning:
                    sink(f"경고: {result.warning}")

            elif action.target == "images":
                account = await writes.account_id(self.shell, self.env)
                for cmd in stepsmod.build_commands(self.env, "images",
                                                   {"account_id": account}):
                    outcome = await self.runner.run(cmd, step_id, sink)
                    if not outcome.ok:
                        return outcome
                # From steps, so the region matches what was just pushed to.
                registry = stepsmod.registry_host(self.env, account)
                from installer.core import values
                # Same prefix build_commands just pushed to, not the env default.
                project, _ = envmod.resolve_project(self.env)
                values.save(self.env, {
                    "server_image": f"{registry}/{project}/server:latest",
                    "web_image": f"{registry}/{project}/web:latest",
                })
                sink("tfvars 에 이미지 URI 를 기록했습니다.")

            elif action.target == "local_env":
                mem = await writes.memory_id(self.env, self.shell)
                for result in await writes.write_local_env(self.env, self.shell, mem):
                    sink(f"작성: {result.path}")
                    if result.warning:
                        sink(f"경고: {result.warning}")
            else:
                raise NotImplementedError(action.target)

        except Exception as exc:
            sink(f"실패: {exc}")
            return Result(exit_code=1, cancelled=False, log_path=None, tail=(str(exc),))

        return Result(exit_code=0, cancelled=False, log_path=None, tail=())

    async def cancel_current(self) -> None:
        await self.runner.cancel()

    @property
    def is_running(self) -> bool:
        return self._running_step is not None

    async def action_quit_guarded(self) -> None:
        """Ctrl+C / q cancels a running step instead of killing the app.

        Quitting mid-apply is how a state lock gets left behind.
        """
        if self.is_running:
            await self.cancel_current()
            self.notify(
                "실행을 취소했습니다. terraform apply 였다면 state 잠금이 남았을 수 "
                "있습니다 — 다음 실행에서 force-unlock 안내가 나옵니다.",
                severity="warning",
                timeout=10,
            )
            return
        self.exit()

    async def action_refresh(self) -> None:
        await self.refresh_states()
        # Redraw in place. Recomposing the screen (the previous approach) built
        # a fresh DataTable with no columns while on_mount did not run again, so
        # the next row insert raised and the app exited — verified in a TTY.
        refill = getattr(self.screen, "refill", None)
        if callable(refill):
            refill()
