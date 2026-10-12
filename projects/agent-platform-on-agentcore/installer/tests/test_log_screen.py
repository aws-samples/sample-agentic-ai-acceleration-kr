"""
Tests for failure presentation.

format_failure is pure so the wording can be tested without a terminal. A failed
step must surface three things — exit code, the tail of the output, and the path
to the full log — because a truncated widget is not enough to diagnose an apply
that died 200 lines ago. Recognised errors gain an interpretation; unrecognised
ones must still show their output rather than a generic apology.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core.runner import Result  # noqa: E402
from installer.screens.logs import format_failure  # noqa: E402


def test_failure_shows_exit_code_tail_and_log_path(tmp_path):
    log = tmp_path / "20260808-120000-first_apply.log"
    log.write_text("...")
    text = format_failure(
        Result(exit_code=1, cancelled=False, log_path=log,
               tail=("Error: something broke", "more context"))
    )
    assert "exit 1" in text
    assert "Error: something broke" in text
    assert str(log) in text


def test_recognised_error_gets_an_interpretation(tmp_path):
    text = format_failure(
        Result(exit_code=1, cancelled=False, log_path=tmp_path / "a.log",
               tail=("Error acquiring the state lock",
                     "  ID:        abc12345-0000-0000-0000-000000000000"))
    )
    assert "force-unlock" in text
    assert "abc12345-0000-0000-0000-000000000000" in text


def test_unrecognised_error_still_shows_its_output(tmp_path):
    text = format_failure(
        Result(exit_code=2, cancelled=False, log_path=tmp_path / "a.log",
               tail=("Error: a brand new failure nobody mapped",))
    )
    assert "a brand new failure nobody mapped" in text


def test_cancellation_warns_about_the_state_lock(tmp_path):
    text = format_failure(
        Result(exit_code=-15, cancelled=True, log_path=tmp_path / "a.log", tail=())
    )
    assert "취소" in text
    assert "잠금" in text


def test_success_needs_no_failure_text(tmp_path):
    assert format_failure(
        Result(exit_code=0, cancelled=False, log_path=tmp_path / "a.log", tail=())
    ) == ""
