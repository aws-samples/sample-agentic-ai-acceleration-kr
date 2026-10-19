"""Interactively preview the UI with fake AWS results and temporary settings.

    python -m installer.tools.preview

Uses the screenshot fixtures. Commands are only displayed, and settings saved
with Ctrl+S go to a temporary directory that is removed when the preview exits.
"""
from pathlib import Path
from tempfile import TemporaryDirectory

from installer.tools.screenshots import DONE, _isolate_tfvars, _make_app


def main() -> None:
    with TemporaryDirectory(prefix="installer-ui-preview-") as directory:
        _isolate_tfvars(Path(directory))
        app = _make_app(DONE, dry_run=True)
        app.title = "Installer UI 미리보기 — AWS 호출 없음 / 임시 설정"
        app.run()


if __name__ == "__main__":
    main()
