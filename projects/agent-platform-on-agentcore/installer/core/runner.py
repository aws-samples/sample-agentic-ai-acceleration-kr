"""Subprocess execution: the only place the installer spawns a process."""
from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

TAIL_LINES = 40


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: dict[str, str] | None = None

    def display(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)


@dataclass(frozen=True)
class Result:
    exit_code: int
    cancelled: bool
    log_path: Path | None
    tail: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.cancelled


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
    """Signal proc's process group. False when there is nothing left to signal."""
    if proc.returncode is not None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        return False
    return True


async def _reap(proc: asyncio.subprocess.Process, grace: float) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        _signal_group(proc, signal.SIGKILL)
        await proc.wait()


async def terminate_group(proc: asyncio.subprocess.Process, grace: float = 5) -> None:
    """SIGTERM the process group, then SIGKILL anything that ignores it.

    terraform spawns provider plugins, so the parent alone is not enough. It
    treats SIGTERM as an interrupt — stops, saves state, releases the lock
    (verified on terraform 1.16 with a local backend); the SIGKILL fallback does
    not get that chance. The process must have been started with
    start_new_session=True, or the group is the installer's own.
    """
    if _signal_group(proc, signal.SIGTERM):
        await _reap(proc, grace)


async def terminate_on_cancel(proc: asyncio.subprocess.Process) -> None:
    """Stop proc from an `except CancelledError` block, before re-raising.

    A cancelled task used to leave its process running: a re-pressed Enter left
    `terraform destroy` going with nothing on screen, and an abandoned
    `plan -destroy` blocked on a pipe nobody read, holding the state lock until
    the TUI exited. The signal goes out synchronously, so even a loop that is
    shutting down delivers it; the wait is shielded and retried so a second
    cancel cannot let the caller move on while the process is still alive.
    """
    if not _signal_group(proc, signal.SIGTERM):
        return
    reaping = asyncio.ensure_future(_reap(proc, grace=5))
    while not reaping.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(reaping)


class Runner:
    """Runs one command at a time and can cancel it by process group."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False

    async def run(
        self, cmd: Command, step_id: str, on_line: Callable[[str], None]
    ) -> Result:
        self._cancelled = False
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{step_id}.log"

        tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)
        environ = {**os.environ, **(cmd.env or {})}

        # Open 0600 from the start: terraform output can carry tfvars values.
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as log:
            log.write(f"$ {cmd.display()}\n")
            log.flush()

            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd.argv,
                    cwd=str(cmd.cwd) if cmd.cwd else None,
                    env=environ,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,  # merged: preserves order
                    start_new_session=True,            # own process group, for killpg
                )
            except OSError as exc:
                message = f"실행할 수 없습니다: {cmd.argv[0]} ({exc})"
                on_line(message)
                log.write(message + "\n")
                return Result(exit_code=127, cancelled=False, log_path=log_path,
                              tail=(message,))

            proc = self._proc
            assert proc.stdout is not None
            try:
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip("\n")
                    tail.append(line)
                    log.write(line + "\n")
                    on_line(line)

                exit_code = await proc.wait()
            except asyncio.CancelledError:
                # The task was cancelled (its screen closed, the app is exiting):
                # stop the command too rather than leave it running unseen.
                log.write("[installer] 작업이 취소되어 명령을 종료했습니다.\n")
                await terminate_on_cancel(proc)
                raise
            finally:
                self._proc = None

        return Result(
            exit_code=exit_code,
            cancelled=self._cancelled,
            log_path=log_path,
            tail=tuple(tail),
        )

    async def cancel(self) -> None:
        """Stop the running command's whole process group (see terminate_group)."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._cancelled = True
        await terminate_group(proc)
