"""
Tests for installer argument parsing and TTY guard.

The installer deploys one environment, infra/envs/standalone, so there is no
`--env` flag: a flag with one valid value only invites typos. These tests pin
that and reject the old flag rather than silently ignoring it.

TTY guard tests use monkeypatch to avoid spawning processes and verify that
_tty_available() correctly detects missing TTY, stdout, or TERM before
attempting to start the Textual app.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.__main__ import _tty_available, parse_args  # noqa: E402


def test_defaults_to_standalone():
    args = parse_args([])
    assert args.env == "standalone"
    assert args.dry_run is False


def test_dry_run_flag():
    assert parse_args(["--dry-run"]).dry_run is True


def test_env_flag_is_gone():
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--env", "standalone"])
    assert exc_info.value.code == 2


# TTY guard tests: _tty_available() blocks non-interactive stdin/stdout and missing TERM
def test_tty_available_requires_stdin_isatty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm")
    assert _tty_available() is False


def test_tty_available_requires_stdout_isatty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setenv("TERM", "xterm")
    assert _tty_available() is False


def test_tty_available_requires_term_env(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("TERM", raising=False)
    assert _tty_available() is False


def test_tty_available_when_all_conditions_met(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm")
    assert _tty_available() is True
