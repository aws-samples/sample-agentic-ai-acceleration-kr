"""SessionCache(LRU + 크기 상한) 순수 로직 테스트."""

import pytest

from orchestrator.session_cache import SessionCache


def test_put_and_get_hit():
    cache = SessionCache(max_size=4)
    assert cache.put("s1", "entry1") == []
    assert cache.get("s1") == "entry1"
    assert "s1" in cache


def test_get_miss_returns_none():
    cache = SessionCache()
    assert cache.get("nope") is None
    assert "nope" not in cache


def test_pop_removes_entry():
    cache = SessionCache()
    cache.put("s1", "e1")
    assert cache.pop("s1") == "e1"
    assert cache.get("s1") is None
    assert cache.pop("s1") is None


def test_replace_same_key_returns_old_entry():
    cache = SessionCache()
    cache.put("s1", "old")
    evicted = cache.put("s1", "new")
    assert evicted == ["old"]
    assert cache.get("s1") == "new"
    assert len(cache) == 1


def test_size_cap_evicts_lru():
    cache = SessionCache(max_size=2)
    cache.put("s1", "e1")
    cache.put("s2", "e2")
    # s1 을 접근해 최근 사용으로 갱신 → s2 가 LRU 가 됨
    assert cache.get("s1") == "e1"
    evicted = cache.put("s3", "e3")
    assert evicted == ["e2"]
    assert "s2" not in cache
    assert "s1" in cache
    assert "s3" in cache


def test_size_cap_evicts_oldest_without_access():
    cache = SessionCache(max_size=2)
    cache.put("s1", "e1")
    cache.put("s2", "e2")
    evicted = cache.put("s3", "e3")
    # 접근이 없었으므로 가장 먼저 넣은 s1 이 축출
    assert evicted == ["e1"]
    assert "s1" not in cache


def test_len_reflects_contents():
    cache = SessionCache()
    assert len(cache) == 0
    cache.put("a", 1)
    cache.put("b", 2)
    assert len(cache) == 2


def test_invalid_max_size_raises():
    with pytest.raises(ValueError, match="max_size"):
        SessionCache(max_size=0)
