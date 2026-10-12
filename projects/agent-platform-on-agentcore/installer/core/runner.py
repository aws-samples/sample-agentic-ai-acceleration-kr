"""Subprocess execution: the only place the installer spawns a process."""
from __future__ import annotations

import asyncio
import collections
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

            assert self._proc.stdout is not None
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                tail.append(line)
                log.write(line + "\n")
                on_line(line)

            exit_code = await self._proc.wait()

        self._proc = None
        return Result(
            exit_code=exit_code,
            cancelled=self._cancelled,
            log_path=log_path,
            tail=tuple(tail),
        )

    async def cancel(self) -> None:
        """SIGTERM the process group, then SIGKILL anything that ignores it.

        terraform spawns provider plugins, so the parent alone is not enough.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._cancelled = True
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
