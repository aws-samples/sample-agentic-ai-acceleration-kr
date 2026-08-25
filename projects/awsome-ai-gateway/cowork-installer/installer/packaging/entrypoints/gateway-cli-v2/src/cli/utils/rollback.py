# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""In-process, invocation-scoped rollback for a multi-step ``setup``.

``setup`` mutates several independent surfaces in sequence (corporate CA trust
store, then the managed-config registry/file). A failure partway through leaves a
partial state that nothing undoes automatically. The durable markers/backups on
disk let a *later* ``clear`` recover from a crash, but a forward-path failure
never triggers that path.

:class:`Rollback` is the **in-process, invocation-scoped** tier that complements
(does not replace) that durable tier. It is a LIFO compensating-transaction saga:
each forward step arms an undo callable **before** its first live mutation
(forward steps are not atomic), and on any exception the armed compensations run
in reverse, best-effort. On success the caller calls :meth:`commit` and nothing
unwinds.

Three correctness properties this encodes (see
``docs/cowork-setup-rollback-design.md`` §5):

1. **Checked results.** A compensation returns a result object with an ``.ok``
   bool; the loop treats ``ok == False`` the SAME as a raised exception — a
   partial state that must be surfaced, never logged as success. The underlying
   primitives (``remove_config``/``ca.restore``-style calls) report operational
   failure by *return value*, not by raising, so catching exceptions alone would
   silently swallow a failed rollback.
2. **Arm before mutation.** Callers must ``arm()`` a compensation before the
   step's first live write, so a step that partially mutates and *then* fails is
   still covered.
3. **Best-effort unwind.** One failing compensation never aborts the rest; all
   failures are collected in :attr:`rollback_errors` and the ORIGINAL exception is
   re-raised (it is the primary failure the user needs to see).

This tier reverts LIVE state only; it deliberately leaves the durable first-setup
snapshot on disk so crash-recovery via ``clear`` still works even if the process
dies mid-rollback. The compensations armed here MUST be invocation-scoped (revert
only *this* run's delta) — never the global ``clear``/``restore`` primitives,
which revert to the first-setup baseline and would wipe a previously-successful
setup on a failed retry.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

import structlog

log = structlog.get_logger(component="rollback")


class CompensationFailed(Exception):
    """A compensation reported ``ok == False`` (returned a result, did not raise).

    Not raised by :class:`Rollback` itself (it collects rather than raises during
    unwind); provided so callers/tests can signal or assert on a not-ok
    compensation result explicitly.
    """


@runtime_checkable
class HasOk(Protocol):
    """Structural type for a compensation's return value.

    A compensation returns anything with an ``.ok`` bool (and, ideally, a
    ``.detail`` str). ``ConfigResult`` and ``CaResult`` already satisfy this.
    """

    ok: bool


class Rollback:
    """LIFO compensation stack for a multi-step, invocation-scoped operation.

    Usage::

        with Rollback() as rb:
            rb.arm("corporate CA", ca_undo.compensate)   # BEFORE the mutation
            ca_undo.apply()
            rb.arm("managed config", cfg_undo.compensate)
            result = cfg_undo.apply()
            if not result.ok:
                raise click.ClickException(result.detail)
            rb.commit()                                   # success: nothing unwinds

    On a normal (non-exception) exit *after* :meth:`commit`, nothing runs. On an
    exception with no commit, the stack unwinds in reverse: every compensation
    runs even if an earlier one fails; failures (a raised exception OR an ``ok ==
    False`` result) are collected in :attr:`rollback_errors`, and the ORIGINAL
    exception is re-raised.
    """

    def __init__(self) -> None:
        self._undo: list[tuple[str, Callable[[], object]]] = []
        self._committed = False
        # (label, detail) for each compensation that raised or reported not-ok.
        self.rollback_errors: list[tuple[str, str]] = []
        # (label) for each compensation that reported ok — for reporting.
        self.rolled_back: list[str] = []

    def arm(self, label: str, undo: Callable[[], object]) -> None:
        """Register a compensation. Call BEFORE the step's first live mutation.

        ``undo`` is a zero-arg callable returning an object with an ``.ok`` bool
        (a raise is also treated as failure). ``label`` names the step for the
        rollback report.
        """
        self._undo.append((label, undo))

    def commit(self) -> None:
        """Mark the operation successful — the stack will not unwind on exit."""
        self._committed = True

    def __enter__(self) -> "Rollback":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Success (no exception) OR an explicit commit → keep everything, unwind
        # nothing. We only roll back when an exception is propagating and the
        # operation was never committed.
        if exc_type is None or self._committed:
            return False
        for label, undo in reversed(self._undo):
            try:
                result = undo()
                # §5 rule 3 — checked result: not-ok is a failure, same as a raise.
                if not getattr(result, "ok", False):
                    detail = getattr(result, "detail", "no detail")
                    self.rollback_errors.append((label, detail))
                    log.error("rollback_step_failed", step=label, detail=detail)
                else:
                    self.rolled_back.append(label)
                    log.info("rollback_step_ok", step=label)
            except Exception as exc_undo:  # noqa: BLE001 — best-effort; collect + continue
                self.rollback_errors.append((label, str(exc_undo)))
                log.error("rollback_step_raised", step=label, error=str(exc_undo))
        return False  # re-raise the ORIGINAL failure — it is the primary signal
