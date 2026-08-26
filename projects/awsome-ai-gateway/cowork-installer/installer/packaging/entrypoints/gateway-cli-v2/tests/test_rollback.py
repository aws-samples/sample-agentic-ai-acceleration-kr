# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for the invocation-scoped :class:`Rollback` saga.

Covers the three correctness properties the adversarial review forced into the
design (docs/cowork-setup-rollback-design.md §8):
  #1 checked results — a compensation returning ``ok == False`` is a FAILURE;
  #2 arm-before-mutation — compensations armed before a fault still run;
  #3 best-effort unwind + original-exception-is-primary.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cli.utils.rollback import CompensationFailed, Rollback


@dataclass
class _Result:
    """Minimal stand-in for ConfigResult/CaResult (has .ok + .detail)."""

    ok: bool
    detail: str = ""


def test_commit_skips_unwind():
    calls = []
    with Rollback() as rb:
        rb.arm("step1", lambda: calls.append("undo1") or _Result(True))
        rb.commit()
    assert calls == []  # nothing unwound on success
    assert rb.rollback_errors == []
    assert rb.rolled_back == []


def test_clean_exit_without_commit_does_not_unwind():
    # No exception AND no commit — the context still exited normally, so keep state.
    calls = []
    with Rollback() as rb:
        rb.arm("step1", lambda: calls.append("undo1") or _Result(True))
        # deliberately no commit()
    assert calls == []


def test_exception_unwinds_in_lifo_order():
    order = []
    with pytest.raises(RuntimeError, match="boom"):
        with Rollback() as rb:
            rb.arm("first", lambda: order.append("first") or _Result(True))
            rb.arm("second", lambda: order.append("second") or _Result(True))
            raise RuntimeError("boom")
    assert order == ["second", "first"]  # LIFO
    assert rb.rolled_back == ["second", "first"]
    assert rb.rollback_errors == []


def test_original_exception_is_reraised():
    class MyError(Exception):
        pass

    with pytest.raises(MyError):
        with Rollback() as rb:
            rb.arm("s", lambda: _Result(True))
            raise MyError("the primary failure")


def test_not_ok_result_reported_as_failure():
    # §8 #1 — a compensation that returns ok=False (does NOT raise) must be recorded
    # as a rollback failure, never logged as success.
    with pytest.raises(RuntimeError):
        with Rollback() as rb:
            rb.arm("bad", lambda: _Result(False, "restore denied"))
            raise RuntimeError("trigger")
    assert rb.rolled_back == []
    assert rb.rollback_errors == [("bad", "restore denied")]


def test_raising_compensation_collected_and_continues():
    # §8 best-effort — one compensation raising must not abort the others.
    order = []

    def raising():
        order.append("raiser")
        raise ValueError("kaboom")

    def ok():
        order.append("ok")
        return _Result(True)

    with pytest.raises(RuntimeError):
        with Rollback() as rb:
            rb.arm("ok-step", ok)          # runs second (LIFO), should still run
            rb.arm("raise-step", raising)  # runs first, raises
            raise RuntimeError("trigger")
    assert order == ["raiser", "ok"]              # both ran despite the raise
    assert rb.rolled_back == ["ok-step"]
    assert rb.rollback_errors == [("raise-step", "kaboom")]


def test_mixed_ok_and_notok_and_raise_all_reported():
    with pytest.raises(RuntimeError):
        with Rollback() as rb:
            rb.arm("good", lambda: _Result(True))
            rb.arm("notok", lambda: _Result(False, "nope"))
            rb.arm("raise", lambda: (_ for _ in ()).throw(OSError("io")))
            raise RuntimeError("trigger")
    # unwind order: raise, notok, good
    assert rb.rolled_back == ["good"]
    assert ("notok", "nope") in rb.rollback_errors
    assert any(label == "raise" and "io" in detail for label, detail in rb.rollback_errors)


def test_none_result_treated_as_failure():
    # A compensation returning None (no .ok attribute) is treated as not-ok (§8 #1).
    with pytest.raises(RuntimeError):
        with Rollback() as rb:
            rb.arm("noneish", lambda: None)
            raise RuntimeError("trigger")
    assert rb.rolled_back == []
    assert rb.rollback_errors and rb.rollback_errors[0][0] == "noneish"


def test_compensation_failed_exception_exists():
    # The design names a dedicated exception for callers/tests that want to signal
    # a not-ok compensation explicitly.
    assert issubclass(CompensationFailed, Exception)
