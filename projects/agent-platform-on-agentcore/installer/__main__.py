"""Entry point for the infrastructure installer TUI."""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

ENV = "standalone"


@dataclass(frozen=True)
class Args:
    env: str
    dry_run: bool


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        prog="install.sh",
        description="agent-platform 인프라 배포 TUI",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="명령을 실행하지 않고 조립된 커맨드만 보여준다",
    )
    ns = parser.parse_args(argv)
    return Args(env=ENV, dry_run=ns.dry_run)


def _tty_available() -> bool:
    """Textual needs a real terminal on both ends.

    Detected up front so a piped or CI invocation gets a sentence of plain text
    instead of a Textual traceback.
    """
    return sys.stdin.isatty() and sys.stdout.isatty() and bool(os.environ.get("TERM"))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if not _tty_available():
        print(
            "이 인스톨러는 터미널에서 직접 실행해야 합니다 "
            "(TUI를 띄울 수 없는 환경입니다).\n"
            "파이프나 CI에서는 DEPLOYMENT.md 의 수동 절차를 쓰십시오.",
            file=sys.stderr,
        )
        return 2

    from installer.app import InstallerApp  # imported late: pulls in textual

    InstallerApp(env=args.env, dry_run=args.dry_run).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
