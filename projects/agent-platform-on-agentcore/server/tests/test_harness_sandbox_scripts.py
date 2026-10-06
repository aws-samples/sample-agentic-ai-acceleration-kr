"""The two programs that run inside the harness sandbox.

They are executed here with the same interpreter version the sandbox has
(python3.12) against a real temp directory, because the interesting cases are all
filesystem and encoding cases: Korean filenames, symlinks, a file over the cap,
an extension not on the allowlist. A stub of `os.walk` would test none of them.

The sandbox has no `find`, so the listing is a python program rather than a shell
pipeline, and paths travel base64-encoded so a name containing a quote or a space
cannot break the JSON or the command string.
"""
import base64
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.harness_sandbox_scripts import (  # noqa: E402
    LIST_SCRIPT,
    UPLOAD_SCRIPT,
    decode_output,
    encode_args,
    list_command,
    upload_command,
)


def run_script(script: str, payload: dict) -> dict:
    """Execute a sandbox program the way the sandbox will: stdin + one b64 argv."""
    proc = subprocess.run(
        [sys.executable, "-", encode_args(payload)],
        input=script,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return decode_output(proc.stdout)


def b64(path: str) -> str:
    return base64.b64encode(path.encode("utf-8")).decode("ascii")


def paths_of(result: dict) -> set:
    return {base64.b64decode(f["p"]).decode("utf-8") for f in result["files"]}


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "보고서.docx").write_bytes(b"docx-bytes")
    (tmp_path / "build.py").write_text("print('helper')")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sheet.xlsx").write_bytes(b"xlsx-bytes")
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "internal.md").write_text("harness internals")
    (tmp_path / ".hidden.pdf").write_bytes(b"dot")
    (tmp_path / "huge.pdf").write_bytes(b"x" * 2048)
    os.symlink(tmp_path / "보고서.docx", tmp_path / "link.docx")
    return tmp_path


def listing(sandbox, **overrides):
    payload = {
        "roots": [str(sandbox)],
        "extensions": ["docx", "xlsx", "pdf"],
        "max_file_bytes": 1024,
        "max_files": 10,
        "max_depth": 3,
    }
    payload.update(overrides)
    return run_script(LIST_SCRIPT, payload)


def test_lists_allowlisted_files_including_korean_names(sandbox):
    found = paths_of(listing(sandbox))
    assert str(sandbox / "보고서.docx") in found
    assert str(sandbox / "data" / "sheet.xlsx") in found


def test_excludes_helpers_dotfiles_agents_dir_and_symlinks(sandbox):
    found = paths_of(listing(sandbox))
    assert str(sandbox / "build.py") not in found          # not on the allowlist
    assert str(sandbox / ".hidden.pdf") not in found       # dotfile
    assert str(sandbox / ".agents" / "internal.md") not in found  # harness internals
    assert str(sandbox / "link.docx") not in found         # symlink


def test_oversized_file_is_reported_not_silently_dropped(sandbox):
    result = listing(sandbox)
    assert str(sandbox / "huge.pdf") not in paths_of(result)
    reasons = {
        base64.b64decode(d["p"]).decode("utf-8"): d["why"] for d in result["dropped"]
    }
    assert reasons[str(sandbox / "huge.pdf")] == "too_large"


def test_file_limit_keeps_newest_and_reports_the_rest(sandbox):
    result = listing(sandbox, max_files=1)
    assert len(result["files"]) == 1
    assert any(d["why"] == "over_file_limit" for d in result["dropped"])


def test_size_and_mtime_are_reported(sandbox):
    entry = next(
        f
        for f in listing(sandbox)["files"]
        if base64.b64decode(f["p"]).decode("utf-8").endswith("보고서.docx")
    )
    assert entry["s"] == len(b"docx-bytes")
    assert entry["m"] == int((sandbox / "보고서.docx").stat().st_mtime)


def test_missing_root_is_not_an_error(tmp_path):
    result = run_script(
        LIST_SCRIPT,
        {
            "roots": [str(tmp_path / "nope")],
            "extensions": ["docx"],
            "max_file_bytes": 1024,
            "max_files": 10,
            "max_depth": 3,
        },
    )
    assert result == {"files": [], "dropped": []}


def test_upload_script_puts_each_file_and_reports_status(sandbox, monkeypatch):
    """The upload program is exercised against a local HTTP server standing in for S3."""
    import http.server
    import threading

    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            length = int(self.headers["Content-Length"])
            received[self.path] = self.rfile.read(length)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    result = run_script(
        UPLOAD_SCRIPT,
        {
            "uploads": [
                {"p": b64(str(sandbox / "보고서.docx")), "url": f"{base}/report"},
                {"p": b64(str(sandbox / "data" / "sheet.xlsx")), "url": f"{base}/sheet"},
            ],
            "timeout": 10,
        },
    )
    server.shutdown()

    assert [u["status"] for u in result["uploads"]] == [200, 200]
    assert received["/report"] == b"docx-bytes"
    assert received["/sheet"] == b"xlsx-bytes"


def test_upload_script_reports_a_failure_without_dying(sandbox):
    result = run_script(
        UPLOAD_SCRIPT,
        {
            "uploads": [
                {"p": b64(str(sandbox / "보고서.docx")), "url": "http://127.0.0.1:1/x"}
            ],
            "timeout": 2,
        },
    )
    assert "error" in result["uploads"][0]


def test_command_strings_are_pure_base64_and_shell_safe():
    """A filename can never change the shape of the command.

    Everything variable is base64, which contains only [A-Za-z0-9+/=], so no
    quote, space, `$` or backtick from a filename can reach the shell.
    """
    command = upload_command(
        [{"p": b64("/home/보 고서'; rm -rf /.docx"), "url": "https://s3/x?sig=1"}]
    )
    assert "rm -rf" not in command
    assert command.startswith("/bin/bash -c '")
    assert command.endswith("'")
    assert command.count("'") == 2  # only the wrapper's own quotes


def test_list_command_carries_the_configured_limits():
    command = list_command(["/home"], ["docx"], 1234, 7)
    payload = json.loads(base64.b64decode(command.split()[-1].rstrip("'")))
    assert payload["roots"] == ["/home"]
    assert payload["max_file_bytes"] == 1234
    assert payload["max_files"] == 7
    assert payload["max_depth"] == 3
