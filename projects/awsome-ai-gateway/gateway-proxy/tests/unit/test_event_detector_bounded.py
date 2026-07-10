# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for SecurityEventDetector bounded memory (견고성 6축검증 축⑥).

과거엔 인증 실패마다 dict[ip] 키가 무한 누적됐다(clear()는 리스트만 비우고 키는 잔존).
스푸핑 x-forwarded-for churn 하에서 워커별 메모리 OOM 위험 → LRU 상한 + 빈 키 eviction 으로
dict 크기를 bounded 로 유지한다. 기능(스파이크 임계 감지·이벤트 발행)은 회귀 없어야 한다.
"""

from __future__ import annotations

import pytest

from app.schemas.domain import AuthType
from app.security.event_detector import SecurityEventDetector


@pytest.mark.asyncio
async def test_key_evicted_after_event_publish():
    # 임계 도달로 이벤트 발행되면 해당 IP 키가 dict 에서 제거돼야 한다(빈 리스트 잔존 X).
    det = SecurityEventDetector(window_sec=300, threshold=3)
    for _ in range(3):
        await det.record_auth_failure("1.2.3.4", AuthType.VIRTUAL_KEY)
    assert "1.2.3.4" not in det._counters  # 발행 후 회수


@pytest.mark.asyncio
async def test_lru_cap_bounds_dict_size():
    # 상한(max_tracked_ips)을 넘는 고유 IP 를 쏟아부어도 dict 크기가 상한으로 bounded.
    cap = 50
    det = SecurityEventDetector(window_sec=300, threshold=1000, max_tracked_ips=cap)
    for i in range(cap * 4):
        await det.record_auth_failure(f"10.0.{i // 256}.{i % 256}", AuthType.VIRTUAL_KEY)
    assert len(det._counters) <= cap


@pytest.mark.asyncio
async def test_lru_evicts_oldest_first():
    det = SecurityEventDetector(window_sec=300, threshold=1000, max_tracked_ips=2)
    await det.record_auth_failure("a", AuthType.VIRTUAL_KEY)
    await det.record_auth_failure("b", AuthType.VIRTUAL_KEY)
    await det.record_auth_failure("c", AuthType.VIRTUAL_KEY)  # a 가 밀려나야
    assert "a" not in det._counters
    assert "b" in det._counters and "c" in det._counters


@pytest.mark.asyncio
async def test_recent_use_moves_to_end():
    # 재사용된 키는 LRU 뒤로 이동 → evict 대상에서 보호.
    det = SecurityEventDetector(window_sec=300, threshold=1000, max_tracked_ips=2)
    await det.record_auth_failure("a", AuthType.VIRTUAL_KEY)
    await det.record_auth_failure("b", AuthType.VIRTUAL_KEY)
    await det.record_auth_failure("a", AuthType.VIRTUAL_KEY)  # a 재사용 → 최신
    await det.record_auth_failure("c", AuthType.VIRTUAL_KEY)  # b 가 밀려나야(a 아님)
    assert "b" not in det._counters
    assert "a" in det._counters and "c" in det._counters


@pytest.mark.asyncio
async def test_spike_still_detected_and_published():
    # 회귀 방지: 임계 도달 시 여전히 Redis publish 가 호출된다.
    published = []

    class _Redis:
        async def publish(self, ch, payload):
            published.append((ch, payload))

    det = SecurityEventDetector(window_sec=300, threshold=5)
    det.set_redis(_Redis())
    for _ in range(5):
        await det.record_auth_failure("9.9.9.9", AuthType.VIRTUAL_KEY)
    assert len(published) == 1
    assert published[0][0] == "notifications:security"


@pytest.mark.asyncio
async def test_below_threshold_keeps_counting():
    det = SecurityEventDetector(window_sec=300, threshold=10)
    for _ in range(4):
        await det.record_auth_failure("5.5.5.5", AuthType.VIRTUAL_KEY)
    assert len(det._counters["5.5.5.5"]) == 4  # 아직 임계 미달 → 유지
