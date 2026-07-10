# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Dead-letter spool for cost:stream XADD failures (P0-②).

The ONLY durable path that carries a finalized request's usage to the DB is the
``cost:stream`` Redis Stream. If Redis is down at finalize time, the XADD fails
and — previously — the record was lost forever (the failure was swallowed with a
warning). This spool catches those failed payloads in a bounded in-memory buffer
and re-publishes them when Redis recovers (health checker) or on shutdown.

Scope / limits (honest):
  - In-process memory only. A pod crash loses whatever is still spooled — this is
    a "seconds-to-minutes blip" mitigation, NOT a cross-restart guarantee. A fully
    durable spool would need an out-of-Redis store (the audit's longer-term item).
  - Bounded (drop-oldest) to avoid unbounded growth during a long outage; drops
    are counted + logged so the loss is visible rather than silent.
  - Re-publish is plain re-XADD; the cost-recorder-worker's usage_logs INSERT is
    idempotent (request_id UNIQUE) and budget_usages is now replay-safe (P0-①),
    so a re-published entry cannot double-count even if it raced a partial XADD.

This is distinct from ``UsageBufferQueue`` (which is DB/ORM-shaped, for a separate
DB-outage concern); this spool re-publishes to the SAME stream the worker reads.
"""
from __future__ import annotations

import asyncio
from collections import deque

import structlog

logger = structlog.get_logger(__name__)


class CostStreamSpool:
    """Bounded in-memory buffer of cost:stream XADD payloads pending re-publish."""

    def __init__(self, stream_key: str, maxlen: int = 10_000, maxlen_field: int = 100_000) -> None:
        self._stream_key = stream_key
        self._maxlen = maxlen
        self._maxlen_field = maxlen_field  # XADD MAXLEN approx trim on re-publish
        # PLAIN deque (NOT deque(maxlen=...)). The bound is enforced explicitly in
        # enqueue() so that overflow eviction is a deliberate, counted action — and
        # crucially so that a concurrent append can NEVER auto-evict the in-flight
        # head that drain() has popped into a local variable (see drain()).
        self._buf: deque[str] = deque()
        self._lock = asyncio.Lock()
        self._dropped = 0
        self._drop_counter = None  # GatewayMetrics.usage_records_dropped_total

    def set_metrics(self, dropped_counter) -> None:
        self._drop_counter = dropped_counter

    @property
    def size(self) -> int:
        return len(self._buf)

    @property
    def dropped(self) -> int:
        return self._dropped

    def enqueue(self, payload_json: str) -> None:
        """Buffer a serialized stream payload (the value passed to XADD).

        Synchronous single-coroutine-step body (no await) — request handlers never
        block on a slow drain. asyncio is single-threaded, so this body runs to
        completion without interleaving with drain()'s synchronous sections.
        When at capacity we evict the OLDEST entry and COUNT it (visible loss, not
        silent). A plain deque is used (no maxlen) so eviction only ever happens
        here, deliberately — never as an automatic side effect of append() racing
        a drain that is holding the in-flight item in a local variable.
        """
        while len(self._buf) >= self._maxlen and self._buf:
            self._buf.popleft()
            self._dropped += 1
            if self._drop_counter is not None:
                self._drop_counter.add(1)
            logger.warning("cost_stream_spool_dropped_oldest", spool_size=len(self._buf))
        self._buf.append(payload_json)

    async def drain(self, redis) -> int:
        """Re-publish buffered payloads to cost:stream. Returns count drained.

        Pop-and-hold: popleft() the head into a LOCAL variable, then await XADD.
        Because the item now lives only in the local (not in the deque), a
        concurrent enqueue() cannot evict it. If publish does not succeed for ANY
        reason the payload is appendleft()'d back; since the deque is unbounded-by-
        construction (bound enforced only in enqueue), that always succeeds.
        The re-buffer is in a `finally` so it ALSO covers asyncio.CancelledError
        (a BaseException — NOT caught by `except Exception`), e.g. the health-check
        task being cancelled on shutdown mid-await: the in-flight payload is put
        back rather than stranded. FIFO preserved; no double-publish (popped
        before XADD, only re-added when not published).
        """
        if redis is None or not self._buf:
            return 0
        drained = 0
        async with self._lock:
            while self._buf:
                payload = self._buf.popleft()  # remove into local; safe from eviction
                published = False
                try:
                    await redis.xadd(
                        self._stream_key,
                        {"payload": payload},
                        maxlen=self._maxlen_field,
                        approximate=True,
                    )
                    published = True
                except Exception:
                    # Redis still unhealthy — finally re-buffers, then we stop.
                    logger.warning(
                        "cost_stream_spool_drain_interrupted",
                        drained=drained,
                        remaining=len(self._buf) + 1,
                    )
                finally:
                    if not published:
                        # Normal failure OR cancellation (CancelledError propagates
                        # after this): never strand the in-flight payload.
                        self._buf.appendleft(payload)
                if not published:
                    break  # normal Exception path; CancelledError already propagated
                drained += 1
        if drained:
            logger.info("cost_stream_spool_drained", count=drained, remaining=len(self._buf))
        return drained
