# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from app.schemas.domain import AuthType, SecurityEvent, SecurityEventType

logger = structlog.get_logger(__name__)

# IP 키 상한. record_auth_failure 는 인증 실패마다 호출되고 source_ip 는 스푸핑 가능한
# x-forwarded-for 라, 무제한 dict 은 스푸핑 IP churn 으로 워커별 메모리 무한 성장(OOM)한다
# (능동 헬스체크로 감지 안 됨 — 견고성 6축검증 축⑥). OrderedDict LRU 로 키 개수를 상한하고,
# 비거나 윈도우 밖으로 만료된 키는 즉시 eviction 한다.
_MAX_TRACKED_IPS = 4096


class SecurityEventDetector:
    """인증 실패 패턴을 감지하여 Redis Pub/Sub으로 보안 이벤트를 발행한다.

    각 uvicorn 워커 프로세스에 독립 인스턴스를 유지한다.
    """

    def __init__(
        self, window_sec: int = 300, threshold: int = 10, max_tracked_ips: int = _MAX_TRACKED_IPS
    ) -> None:
        # LRU: 가장 오래 안 쓰인 IP 키부터 evict (bounded — OOM 방지).
        self._counters: OrderedDict[str, list[float]] = OrderedDict()  # ip -> [timestamps]
        self._window_sec = window_sec
        self._threshold = threshold
        self._max_tracked_ips = max_tracked_ips
        self._redis = None  # 외부에서 주입

    def set_redis(self, redis) -> None:
        self._redis = redis

    async def record_auth_failure(self, source_ip: str, auth_type: AuthType) -> None:
        now = time.time()
        cutoff = now - self._window_sec

        timestamps = self._counters.get(source_ip)
        if timestamps is None:
            timestamps = []
            self._counters[source_ip] = timestamps
        # 최근 사용된 키를 LRU 뒤쪽으로 (evict 대상에서 밀어냄).
        self._counters.move_to_end(source_ip)

        # 윈도우 밖 타임스탬프 제거
        timestamps[:] = [t for t in timestamps if t > cutoff]
        timestamps.append(now)

        if len(timestamps) >= self._threshold:
            event = SecurityEvent(
                event_id=str(uuid4()),
                type=SecurityEventType.AUTH_FAILURE_SPIKE,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                source_ip=source_ip,
                failure_count=len(timestamps),
                window_minutes=self._window_sec // 60,
                auth_type=auth_type,
                details=f"{len(timestamps)} failures in {self._window_sec}s window",
            )
            # 이벤트 발행 후 카운터 리셋 — 리스트를 비우고 **키 자체도 제거**(과거엔
            # clear() 만 해 빈 리스트 키가 dict 에 영구 잔존 → 누적). LRU 상한과 별개로
            # "폭주 후" 즉시 회수.
            self._counters.pop(source_ip, None)

            if self._redis is not None:
                try:
                    await self._redis.publish(
                        "notifications:security",
                        event.model_dump_json(),
                    )
                    logger.warning(
                        "security_event_published",
                        event_type=event.type,
                        source_ip=source_ip,
                    )
                except Exception:
                    logger.exception("security_event_publish_failed", source_ip=source_ip)

        # LRU 상한 강제: 키 개수가 상한을 넘으면 가장 오래 안 쓰인 키부터 evict.
        # (스푸핑 IP churn 하에서도 dict 크기가 _max_tracked_ips 로 bounded)
        # 주의: 이벤트 발행 시 방금 그 IP 는 위에서 이미 pop 됐으므로, 이 루프는 발행 IP 를
        # 다시 건드리지 않는다. pop 순서를 이 루프 뒤로 옮기면 발행 IP 를 evict 후 재삽입하는
        # 미묘한 버그가 생기니 순서 유지할 것.
        while len(self._counters) > self._max_tracked_ips:
            self._counters.popitem(last=False)
