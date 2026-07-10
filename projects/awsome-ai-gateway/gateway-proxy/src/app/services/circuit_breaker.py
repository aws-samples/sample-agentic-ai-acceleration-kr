# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_LUA_PATH = Path(__file__).resolve().parent.parent / "redis_scripts" / "circuit_breaker.lua"


class CircuitBreakerService:
    """Distributed circuit breaker (sliding-window error-rate), keyed per provider_model_id.

    Redis-down => FAIL-OPEN (treat every circuit as closed) to match the rest of the
    gateway's degradation policy: an unavailable breaker must never block inference.
    """

    def __init__(
        self,
        window_sec: int = 30,
        min_calls: int = 5,
        error_rate: float = 0.5,
        open_sec: int = 30,
        halfopen_ttl_ms: int = 8000,
        open_jitter_sec: int = 5,
    ) -> None:
        self.window_sec = window_sec
        self.min_calls = min_calls
        self.error_rate = error_rate
        self.open_sec = open_sec
        self.halfopen_ttl_ms = halfopen_ttl_ms
        self.open_jitter_sec = open_jitter_sec
        self._lua = _LUA_PATH.read_text(encoding="utf-8")

    @staticmethod
    def _keys(pmid: str) -> tuple[str, str, str]:
        return f"cb:{pmid}:fail", f"cb:{pmid}:total", f"cb:{pmid}:open"

    async def is_open(self, redis, pmid: str) -> bool:
        if redis is None:
            return False  # fail-open
        try:
            _f, _t, open_key = self._keys(pmid)
            return bool(await redis.exists(open_key))
        except Exception:
            logger.warning("cb_is_open_failed", pmid=pmid)
            return False  # fail-open

    async def _record(self, redis, pmid: str, is_failure: int) -> bool:
        if redis is None:
            return False
        try:
            fail_key, total_key, open_key = self._keys(pmid)
            sec, micro = await redis.time()  # Redis server time -> ms (avoid host clock skew)
            now_ms = int(sec) * 1000 + int(micro) // 1000
            open_ms = self.open_sec * 1000 + (now_ms % (self.open_jitter_sec * 1000 + 1))  # deterministic jitter seeded by Redis server time; circuit state is shared across pods so per-pod randomness is unnecessary (and would reintroduce host-clock dependence)
            opened = await redis.eval(
                self._lua, 3, fail_key, total_key, open_key,
                str(self.window_sec), str(self.min_calls), str(self.error_rate),
                str(open_ms), str(is_failure), str(now_ms),
            )
            return bool(opened)
        except Exception:
            logger.warning("cb_record_failed", pmid=pmid, is_failure=is_failure)
            return False

    async def record_failure(self, redis, pmid: str) -> bool:
        return await self._record(redis, pmid, 1)

    async def record_success(self, redis, pmid: str) -> bool:
        opened = await self._record(redis, pmid, 0)
        if redis is not None:
            try:
                _f, _t, open_key = self._keys(pmid)
                await redis.delete(open_key)
            except Exception:
                logger.warning("cb_close_failed", pmid=pmid)
        return opened

    async def try_acquire_halfopen_probe(self, redis, pmid: str) -> bool:
        """Elect a single probe across pods via SET NX PX. Winner returns True."""
        if redis is None:
            return True  # fail-open: let the request through
        try:
            won = await redis.set(
                f"cb:{pmid}:halfopen", "1", nx=True, px=self.halfopen_ttl_ms
            )
            return bool(won)
        except Exception:
            logger.warning("cb_halfopen_acquire_failed", pmid=pmid)
            return True
