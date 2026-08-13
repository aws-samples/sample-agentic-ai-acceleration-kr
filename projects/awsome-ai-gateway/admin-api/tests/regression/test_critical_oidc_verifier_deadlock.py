# Copyright 2026 © Amazon.com and Affiliates
"""Regression: #51 — OIDC verifier must not hold a blocking lock across `await`.

Bug: ``_ensure_jwks_async`` held a ``threading.Lock`` while awaiting the JWKS HTTP fetch. On a
single-threaded event loop the second concurrent request called ``lock.acquire()``, which parks
the OS **thread** rather than just the task — so the loop could never resume the first task to
finish its fetch. The whole admin-api worker stopped answering, and it could not recover on its
own: reading the JWKS response would itself require the loop. In production this presented as a
pod that had to be restarted, triggered by nothing more exotic than a cold start plus two
concurrent admin requests.

Fix: ``asyncio.Lock`` + ``async with``. Both halves are required — ``asyncio.Lock`` does not
support the synchronous ``with`` statement, so swapping only the lock type raises TypeError
*before* the fetch. That hides the deadlock symptom while breaking every OIDC login, which is why
this file asserts the fetch still happens rather than merely asserting "no deadlock".

Observation design: a wedged event loop cannot report on itself, so the loop runs in a worker
thread and a heartbeat counter is sampled from the test thread.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import threading
import time

import httpx
import pytest

from app.core.oidc_verifier import OIDCVerifier

ISSUER = "https://example-idp.test/realm"


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


# verify_async rejects malformed tokens before any JWKS fetch, so the token has to be shaped like
# a real JWT to reach the locked section at all.
TOKEN = f'{_b64({"alg": "RS256", "kid": "k1", "typ": "JWT"})}.{_b64({"iss": ISSUER, "sub": "u1"})}.sig'


@pytest.mark.unit
def test_lock_is_asyncio_not_threading():
    """The lock type is the fix. A ``threading.Lock`` here is the bug, by construction."""
    v = OIDCVerifier(issuer_url=ISSUER, audience=None)
    assert isinstance(v._lock, asyncio.Lock), type(v._lock)
    assert not isinstance(v._lock, type(threading.Lock()))


@pytest.mark.unit
def test_lock_is_acquired_with_async_with():
    """`asyncio.Lock` has no ``__enter__``, so a plain ``with self._lock`` would raise TypeError
    at runtime — before the JWKS fetch, making a broken verifier look healthy. Assert the source
    uses ``async with`` so the two halves of the fix can never drift apart."""
    src = inspect.getsource(OIDCVerifier._ensure_jwks_async)
    assert "async with self._lock" in src
    assert not any(
        line.strip().startswith("with self._lock") for line in src.splitlines()
    ), "synchronous `with` on an asyncio.Lock raises TypeError"


class _SlowIdP:
    """Real ASGI IdP whose JWKS response is held open until released — what a cold fetch against
    a remote IdP looks like from the event loop's point of view."""

    def __init__(self) -> None:
        self.release = threading.Event()   # threading: set from the test thread
        self.jwks_hits = 0

    async def __call__(self, scope, receive, send):
        path = scope["path"]
        if path.endswith("/.well-known/openid-configuration"):
            # The verifier requires discovery.issuer to match the configured issuer exactly.
            body = (b'{"issuer": "' + ISSUER.encode() + b'",'
                    b' "jwks_uri": "' + ISSUER.encode() + b'/keys"}')
        elif path.endswith("/keys"):
            self.jwks_hits += 1
            while not self.release.is_set():
                await asyncio.sleep(0.01)      # yield to the loop while waiting
            body = (b'{"keys": [{"kid": "k1", "use": "sig", "kty": "RSA", "alg": "RS256",'
                    b' "n": "AQAB", "e": "AQAB"}]}')
        else:
            body = b"{}"
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


class _LoopHarness:
    """Runs an event loop in a worker thread, exposing a heartbeat the test thread can sample.

    The heartbeat advances only while the loop is scheduling tasks. If the loop's thread is parked
    inside a blocking ``lock.acquire()``, it freezes — the "pod stopped answering" symptom.
    """

    def __init__(self, verifier: OIDCVerifier, idp: _SlowIdP) -> None:
        self.verifier = verifier
        self.idp = idp
        self.heartbeat = 0
        self.ready = threading.Event()
        self._stop = False
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _heart(self) -> None:
        while not self._stop:
            await asyncio.sleep(0.02)
            self.heartbeat += 1

    async def _verify(self, client) -> None:
        try:
            await self.verifier.verify_async(TOKEN, client)
        except Exception:
            pass   # liveness is the measurement here, not the verification outcome

    async def _main(self) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.idp)) as client:
            hb = asyncio.create_task(self._heart())
            self.ready.set()
            a = asyncio.create_task(self._verify(client))
            await asyncio.sleep(0.15)          # let A get inside the lock, awaiting JWKS
            b = asyncio.create_task(self._verify(client))
            await asyncio.gather(a, b, return_exceptions=True)
            self._stop = True
            hb.cancel()
            await asyncio.gather(hb, return_exceptions=True)

    def start(self) -> None:
        self.thread.start()
        self.ready.wait(10)


def _advanced(h: _LoopHarness, seconds: float) -> bool:
    """True if the loop scheduled anything during the window. A thread that has already exited
    is reported as advanced=True, since a finished loop is not a wedge."""
    before = h.heartbeat
    time.sleep(seconds)
    if not h.thread.is_alive():
        return True
    return h.heartbeat != before


@pytest.mark.unit
def test_loop_stays_responsive_during_concurrent_cold_jwks_fetch():
    """Two concurrent verifies on a cold verifier must not stall the event loop.

    With the old blocking lock the heartbeat froze here and never resumed, even after the IdP
    answered.
    """
    idp = _SlowIdP()
    h = _LoopHarness(OIDCVerifier(issuer_url=ISSUER, audience=None), idp)
    h.start()

    time.sleep(0.8)
    assert idp.jwks_hits >= 1, "rig failed: the JWKS fetch never started, so nothing was contended"

    responsive = _advanced(h, 0.5)

    idp.release.set()
    h.thread.join(timeout=15)

    assert responsive, (
        f"event loop stopped scheduling while a JWKS fetch was in flight "
        f"(heartbeat stuck at {h.heartbeat}) — a blocking lock is being held across await"
    )
    assert not h.thread.is_alive(), "both verifies should have completed once the IdP answered"


@pytest.mark.unit
def test_concurrent_verifies_collapse_into_a_single_jwks_fetch():
    """The double-check inside the lock must still do its job: two concurrent cold verifies
    result in exactly one JWKS fetch, not one per request.

    ``jwks_hits == 1`` is also what catches a lock swapped to ``asyncio.Lock`` without changing
    ``with`` to ``async with`` — that variant raises TypeError before fetching, giving 0 hits.
    """
    idp = _SlowIdP()
    h = _LoopHarness(OIDCVerifier(issuer_url=ISSUER, audience=None), idp)
    h.start()
    time.sleep(0.8)
    idp.release.set()
    h.thread.join(timeout=15)

    assert idp.jwks_hits == 1, (
        f"expected the in-lock double-check to collapse two concurrent verifies into one JWKS "
        f"fetch, got {idp.jwks_hits}"
    )
