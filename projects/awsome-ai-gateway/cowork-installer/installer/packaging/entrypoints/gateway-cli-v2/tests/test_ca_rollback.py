# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Invocation-scoping tests for the CA install compensation (§8 #3).

The critical safety property: :meth:`CaInstallPlan.compensate` removes a cert
ONLY if THIS run actually added it (``result.changed``). A CA that was already
trusted, or an install that never ran, must be a no-op — so a failed setup RETRY
never rips out a CA a previous successful run installed. These tests assert that
guard without needing a real trust-store mutation (which requires admin + a pinned
PEM), by driving the plan's recorded state directly.
"""

from __future__ import annotations

from cli import cowork_ca


def test_compensate_noop_before_apply():
    # Never applied → nothing to undo.
    plan = cowork_ca.plan_install()
    result = plan.compensate()
    assert result.ok and not result.changed


def test_compensate_noop_when_ca_already_trusted():
    # apply() ran but reported changed=False (CA was already trusted before this
    # run) → compensate must NOT remove it (§8 #3 — don't wipe a prior install).
    plan = cowork_ca.plan_install()
    plan._result = cowork_ca.CaResult(True, False, "CA already trusted", store="X")
    plan._thumbprint = "AABBCC"
    result = plan.compensate()
    assert result.ok and not result.changed
    assert "nothing to undo" in result.detail


def test_compensate_no_thumbprint_reports_not_ok():
    # This run changed the store but we somehow can't identify the cert → surface a
    # not-ok result (so the rollback loop reports it) rather than silently passing.
    plan = cowork_ca.plan_install()
    plan._result = cowork_ca.CaResult(True, True, "installed", store="X")
    plan._thumbprint = None
    result = plan.compensate()
    assert not result.ok
    assert "manually" in result.detail


def test_apply_missing_pem_is_not_ok_and_compensate_noop(monkeypatch, tmp_path):
    # With no PEM present, apply() returns ok=False/changed=False and the plan's
    # compensation is a no-op (this run installed nothing).
    monkeypatch.setattr(cowork_ca, "resolve_pem", lambda: None)
    monkeypatch.setenv("GATEWAY_CLI_DATA_DIR", str(tmp_path / "data"))
    plan = cowork_ca.plan_install()
    applied = plan.apply()
    assert not applied.ok and not applied.changed
    undo = plan.compensate()
    assert undo.ok and not undo.changed
