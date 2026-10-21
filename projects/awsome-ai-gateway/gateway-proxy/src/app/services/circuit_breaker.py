# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
from __future__ import annotations

from pathlib import Path

import structlog

from app.services.lua_loader import LuaScriptLoader

logger = structlog.get_logger(__name__)

_LUA_PATH = Path(__file__).resolve().parent.parent / "redis_scripts" / "circuit_breaker.lua"


class CircuitBreakerService:
    """Distributed circuit breaker (sliding-window error-rate), keyed per provider_model_id.

    Redis-down => FAIL-OPEN (treat every circuit as closed) to match the rest of the
    gateway's degradation policy: an unavailable breaker must never block inference.

    키는 브레이커마다 같은 hash tag ``cb:{<브레이커 키>}:…`` 다(:meth:`_keys`). 배포 중에는 이
    tag 이전 파드(``cb:<브레이커 키>:…``)와 이후 파드가 서로의 상태를 보지 못한다 — 각 집단이 자기
    실패만 세고 자기 브레이커만 연다. 옛 키는 모두 TTL 이 있다(초 단위 버킷은 window_sec, open 은
    PX open_sec + 지터, halfopen 은 PX). 그래서 배포가 끝나고 1분 안에 사라진다. 클러스터에서는 옛
    파드가 애초에 기록하지 못했으므로(아래) 옛 키가 생기지도 않는다.
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
        # 다른 스크립트와 같이 주석을 빼고 보낸다(LuaScriptLoader 참조 — EVAL 마다 본문이 간다).
        self._lua = LuaScriptLoader.strip_comments(_LUA_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _keys(pmid: str) -> tuple[str, str, str]:
        """한 브레이커의 키 — 모두 같은 hash tag ``{<pmid>}`` 다(halfopen 키도).

        record EVAL 은 이 세 키와 스크립트가 만드는 초 단위 버킷 키(``…:fail:<초>``,
        ``…:total:<초>``)를 함께 쓴다. Redis Cluster 에서는 한 EVAL 의 키가 한 슬롯이어야 해서
        (redis-py 가 보내기 전에 거부한다), tag 가 없던 예전 키(``cb:<pmid>:fail`` …)로는 기록이 한
        번도 성공하지 못했다 — ``cb_record_failed`` 로 삼켜져 클러스터에서 브레이커가 열리지
        않았다(실측). ``pmid`` 는 alias 계정이 있으면 ``<모델 id>@<계정>`` 이고
        (fallback_loop.breaker_key) 그대로 tag 가 된다.
        """
        tag = f"cb:{{{pmid}}}"
        return f"{tag}:fail", f"{tag}:total", f"{tag}:open"

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
                f"cb:{{{pmid}}}:halfopen", "1", nx=True, px=self.halfopen_ttl_ms
            )
            return bool(won)
        except Exception:
            logger.warning("cb_halfopen_acquire_failed", pmid=pmid)
            return True
