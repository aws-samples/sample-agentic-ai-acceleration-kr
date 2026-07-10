# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""실제 Redis Cluster 대상 rate-limit CROSSSLOT 검증 (deepdive Q50 (b)).

**가짜/단일노드 폴백 없음**(사용자 지침). 단일노드는 CROSSSLOT 을 절대 일으키지 않아
mock·standalone 으로는 클러스터 모드 동작(fail-open 으로 enforcement 무음 정지)을 증명
못 한다. 따라서 이 테스트는 **진짜 멀티노드 Redis Cluster** 가 있을 때만 실행되고
(REDIS_CLUSTER_URL 미설정 시 skip), 없으면 가짜로 통과시키지 않는다.

로컬 클러스터 띄우는 법(예): redis:7-alpine 6노드 host-network +
`redis-cli --cluster create 127.0.0.1:7001..7006 --cluster-replicas 1` 후
`REDIS_CLUSTER_URL=redis://127.0.0.1:7001 pytest tests/integration/test_rate_limit_cluster.py`.

검증 내용:
  1) 과거 패턴(다중 hash-tag 키 1 eval) → cross-slot 거부 재현(버그가 실재했음).
  2) 수정된 check_multi_scope_rpm/tpm·reserve_cost 가 클러스터에서 실제 enforce.
  3) fail-open 카운터 0 — 클러스터에서 무음 미집행이 없음(핵심 증거).
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

CLUSTER_URL = os.environ.get("REDIS_CLUSTER_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not CLUSTER_URL,
        reason="REDIS_CLUSTER_URL 미설정 — 진짜 Redis Cluster 필요(가짜/단일노드 금지)",
    ),
]


class _FailOpenSpy:
    def __init__(self) -> None:
        self.count = 0

    def add(self, n, attrs=None) -> None:  # OTel Counter 인터페이스
        self.count += n


@pytest.fixture
def cluster():
    from redis.asyncio.cluster import RedisCluster

    from app.services.lua_loader import LuaScriptLoader

    # 스크립트 로드(앱 startup 과 동일 경로).
    script_dir = Path(__file__).resolve().parents[2] / "src" / "app" / "redis_scripts"
    LuaScriptLoader.load_all(script_dir)
    return RedisCluster.from_url(CLUSTER_URL, socket_timeout=5)


@pytest.mark.asyncio
async def test_old_merged_eval_would_crossslot(cluster):
    """과거 패턴(서로 다른 hash-tag 키를 한 eval)은 클러스터에서 거부된다 = 버그 실재 증거."""
    from redis.exceptions import RedisError

    from app.services.lua_loader import LuaScriptLoader

    try:
        await cluster.eval(
            LuaScriptLoader.get("rate_limit_check"),
            2, "{USER:u1:m1}:rpm", "{TEAM:t1:m1}:rpm",
            "0", "req", "60000", "1", "60", "USER",
        )
        pytest.fail("다중 hash-tag 키 1 eval 이 거부되지 않음 — 클러스터 아님?")
    except RedisError as e:
        msg = str(e)
        assert "CROSSSLOT" in msg.upper() or "hash to the same slot" in msg
    finally:
        await cluster.aclose()


@pytest.mark.asyncio
async def test_split_rpm_enforces_on_cluster_without_fail_open(cluster):
    """분리된 RPM 체크가 클러스터에서 실제 enforce + fail-open 0."""
    from app.services import rate_limit_service as rls
    from app.services.rate_limit_scope import build_scope_descriptors

    spy = _FailOpenSpy()
    rls.set_fail_open_metric(spy)
    try:
        run = uuid.uuid4().hex[:8]
        desc = build_scope_descriptors(
            user_id=f"it-{run}", team_id=f"itt-{run}", model_alias="m",
            user_rpm=3, user_tpm=None, team_rpm=1000, team_tpm=None,
            global_rpm=100000, global_tpm=None,
        )
        svc = rls.RateLimitService()
        seq = [(await svc.check_multi_scope_rpm(cluster, desc)).allowed for _ in range(5)]
        assert seq[:3] == [True, True, True]
        assert seq[3] is False  # limit=3 정확히 enforce(과거엔 fail-open 으로 통과했을 것)
        reject = await svc.check_multi_scope_rpm(cluster, desc)
        assert reject.scope == "USER"
        assert spy.count == 0  # 무음 미집행 없음 — 핵심 증거
    finally:
        rls.set_fail_open_metric(None)
        await cluster.aclose()


@pytest.mark.asyncio
async def test_split_tpm_and_cost_enforce_on_cluster(cluster):
    """TPM·cost 도 클러스터에서 enforce(scope별 단일 슬롯 eval)."""
    from app.services import rate_limit_service as rls
    from app.services.rate_limit_scope import build_scope_descriptors

    spy = _FailOpenSpy()
    rls.set_fail_open_metric(spy)
    try:
        run = uuid.uuid4().hex[:8]
        svc = rls.RateLimitService()

        desc_tpm = build_scope_descriptors(
            user_id=f"itp-{run}", team_id=f"itpt-{run}", model_alias="m",
            user_rpm=None, user_tpm=100, team_rpm=None, team_tpm=100000,
            global_rpm=None, global_tpm=10000000,
        )
        r1 = await svc.check_multi_scope_tpm(cluster, desc_tpm, reserved_tokens=60)
        r2 = await svc.check_multi_scope_tpm(cluster, desc_tpm, reserved_tokens=60)  # 누적 120>100
        assert r1.allowed is True
        assert r2.allowed is False

        c1 = await svc.reserve_cost(
            cluster, user_id=f"itc-{run}", estimated_cost=Decimal("0.6"),
            user_cpm_limit=Decimal("1.0"), user_cph_limit=Decimal("100"),
            team_id=f"itct-{run}", team_cpm_limit=Decimal("100"), team_cph_limit=Decimal("100"),
        )
        c2 = await svc.reserve_cost(
            cluster, user_id=f"itc-{run}", estimated_cost=Decimal("0.6"),
            user_cpm_limit=Decimal("1.0"), user_cph_limit=Decimal("100"),
            team_id=f"itct-{run}", team_cpm_limit=Decimal("100"), team_cph_limit=Decimal("100"),
        )
        assert c1.allowed is True
        assert c2.allowed is False  # 1.2 > 1.0 user cpm
        assert spy.count == 0
    finally:
        rls.set_fail_open_metric(None)
        await cluster.aclose()
