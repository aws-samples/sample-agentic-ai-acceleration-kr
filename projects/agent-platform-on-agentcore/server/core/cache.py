"""Process-wide read caches with single-flight loads.

`ReadThroughCache` serves a value for a TTL and lets one caller load a missing
key while the others wait for it. `StaleWhileRevalidate` also keeps serving the
previous value past its TTL while one background load replaces it, for reads
expensive enough that no request should wait on a refresh (the Insights
dashboards: seconds of CPU each on a 0.5-vCPU task).
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, Hashable, Optional, Tuple

logger = logging.getLogger(__name__)


class _Flight:
    """One in-progress load: the waiters block on `done` and read its outcome."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.error: Optional[BaseException] = None


class ReadThroughCache:
    """Process-wide `key -> value` cache with a TTL and single-flight loads.

    A miss runs `load` in one caller; callers asking for the same key meanwhile
    wait for that load and share its value, or its exception, instead of issuing
    their own. Without this, every request that found the entry expired repeated
    the whole registry listing at the same moment.

    `invalidate()` drops every entry and forgets the loads in flight. A caller
    arriving after a write therefore never waits on a load that began before it,
    and a load that straddles a write is not stored.
    """

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: Dict[Hashable, Tuple[float, Any]] = {}
        self._flights: Dict[Hashable, _Flight] = {}
        self._generation = 0

    def get(
        self,
        key: Hashable,
        load: Callable[[], Any],
        cacheable: Callable[[Any], bool] = lambda value: True,
    ) -> Any:
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and self._clock() - hit[0] < self._ttl:
                return hit[1]
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = _Flight()
                generation = self._generation
        if not leader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value
        try:
            value = load()
        except BaseException as exc:
            flight.error = exc
            self._finish(key, flight, None, generation, store=False)
            raise
        self._finish(key, flight, value, generation, store=cacheable(value))
        return value

    def _finish(
        self, key: Hashable, flight: _Flight, value: Any, generation: int, store: bool
    ) -> None:
        with self._lock:
            if self._flights.get(key) is flight:
                del self._flights[key]
            if store and generation == self._generation:
                self._entries[key] = (self._clock(), value)
        flight.value = value
        flight.done.set()

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._entries.clear()
            self._flights.clear()


class StaleWhileRevalidate:
    """`key -> (computed_at, value)` that answers from memory whenever it can.

    Fresh (younger than `fresh_seconds`): served as is. Stale but younger than
    `max_age_seconds`: served as is, and one background load starts to replace
    it. Older, or absent: the caller loads it (single-flight through a
    ReadThroughCache, so concurrent callers share that load). A failed
    background load keeps the stale value and is retried by the next request.

    At most `max_entries` values are kept. Storing one first drops those past
    `max_age_seconds` (they would never be served again), then the oldest
    computed, so keys built from request input cannot grow the map without end.

    `invalidate()` forgets everything; use it after a write that changes what
    the loads would compute.
    """

    def __init__(
        self,
        fresh_seconds: float,
        max_age_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        spawn: Optional[Callable[[Callable[[], None]], None]] = None,
        max_entries: int = 256,
    ):
        self._fresh = fresh_seconds
        self._max_age = max_age_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._values: Dict[Hashable, Tuple[float, Any]] = {}
        self._refreshing: set = set()
        self._generation = 0
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._loads = ReadThroughCache(0, clock=clock)

    def get(self, key: Hashable, load: Callable[[], Any]) -> Tuple[Any, float]:
        """(value, age in seconds)."""
        now = self._clock()
        with self._lock:
            hit = self._values.get(key)
            generation = self._generation
        if hit is not None:
            age = now - hit[0]
            if age < self._fresh:
                return hit[1], age
            if age < self._max_age:
                self._refresh_in_background(key, load, generation)
                return hit[1], age
        value = self._loads.get(key, load)
        with self._lock:
            if generation == self._generation:
                self._store(key, value)
        return value, 0.0

    def _refresh_in_background(self, key: Hashable, load: Callable[[], Any], generation: int) -> None:
        with self._lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)

        def run() -> None:
            try:
                value = load()
                with self._lock:
                    if generation == self._generation:
                        self._store(key, value)
            except Exception:
                logger.warning("Background refresh failed for %s; serving the previous value", key, exc_info=True)
            finally:
                with self._lock:
                    self._refreshing.discard(key)

        self._spawn(run)

    def _store(self, key: Hashable, value: Any) -> None:
        """Caller holds the lock."""
        now = self._clock()
        self._values[key] = (now, value)
        if len(self._values) <= self._max_entries:
            return
        for old in [k for k, (at, _) in self._values.items() if now - at >= self._max_age]:
            del self._values[old]
        while len(self._values) > self._max_entries:
            del self._values[min(self._values, key=lambda k: self._values[k][0])]

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._values.clear()
        self._loads.invalidate()
