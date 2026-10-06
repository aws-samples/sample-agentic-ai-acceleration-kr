"""The only path secrets take to disk.

Centralised so the rules cannot drift per call site: refuse anything git would
track, 0600, back up before overwrite.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from installer.core import hcl


class WriteRefused(Exception):
    """The target is not safe to write a secret into."""


@dataclass(frozen=True)
class WriteResult:
    path: Path
    warning: str | None
    backup: Path | None


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def is_git_ignored(path: Path) -> bool:
    return _git(["check-ignore", "-q", str(path)], path.parent).returncode == 0


def is_git_tracked(path: Path) -> bool:
    return _git(["ls-files", "--error-unmatch", str(path)], path.parent).returncode == 0


def _check_target(path: Path) -> str | None:
    """Return a warning, or raise if writing here is unsafe."""
    if is_git_ignored(path):
        return None
    if is_git_tracked(path):
        raise WriteRefused(
            f"{path} 는 git 이 추적(tracked)하는 파일이라 비밀값을 쓸 수 없습니다."
        )
    raise WriteRefused(
        f"{path} 는 gitignore 되지 않았습니다. 비밀값이 커밋될 수 있으므로 "
        f"쓰지 않습니다. .gitignore 를 확인하십시오."
    )


def _backup(path: Path) -> tuple[Path | None, str | None]:
    """Copy `path` aside before overwriting — but only where git ignores the copy.

    The target is verified ignored; the `.bak-<stamp>` copy was not, and on a real
    run five backups carrying the admin password showed up as untracked files in
    `git status`, one `git add .` away from a commit. Returns (backup, warning).
    """
    if not path.exists():
        return None, None
    dest = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    if not is_git_ignored(dest):
        return None, (
            f"{dest.name} 은 gitignore 대상이 아니어서 백업을 만들지 않았습니다. "
            f".gitignore 에 `*.bak-*` 를 추가하면 저장 전 백업이 남습니다."
        )
    dest.write_bytes(path.read_bytes())
    os.chmod(dest, 0o600)
    return dest, None


def write_secret_file(path: Path, content: str) -> WriteResult:
    warning = _check_target(path)
    backup, backup_warning = _backup(path)
    if backup_warning:
        warning = backup_warning if warning is None else f"{warning}\n{backup_warning}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, 0o600)
    return WriteResult(path=path, warning=warning, backup=backup)


def set_keys(
    path: Path,
    updates: dict[str, str],
    *,
    style: str,
) -> WriteResult:
    """Replace each key's line in `path`, leaving every other byte alone."""
    text = path.read_text() if path.exists() else ""
    for key, rendered in updates.items():
        if style == "env":
            text = _set_env_key(text, key, rendered)
        else:
            text = hcl.set_var(text, key, rendered)
    return write_secret_file(path, text)


def _set_env_key(text: str, key: str, value: str) -> str:
    """dotenv assignments take no spaces around `=`."""
    import re

    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pattern.search(text):
        return pattern.sub(f"{key}={value}", text, count=1)
    separator = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{separator}{key}={value}\n"
