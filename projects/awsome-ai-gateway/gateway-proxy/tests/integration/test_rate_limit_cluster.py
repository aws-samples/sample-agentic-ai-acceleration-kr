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
  4) GLOBAL 은 모델별이다(0046) — 두 모델의 GLOBAL 키는 hash tag 가 달라 다른 슬롯에
     있고, 모델마다 따로 eval 하므로 각자 한도대로 집행된다. 둘을 한 eval 에 묶으면
     CROSSSLOT 이다(대조군).
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


@pytest.mark.asyncio
async def test_two_models_global_limits_enforce_independently_on_cluster(cluster):
    """모델 a(GLOBAL rpm 2, tpm 100)를 다 써도 모델 b(rpm 3, tpm 1000)는 자기 한도까지 통과."""
    from redis.exceptions import RedisClusterException, RedisError

    from app.services import rate_limit_service as rls
    from app.services.lua_loader import LuaScriptLoader
    from app.services.rate_limit_scope import (
        RateLimitScope,
        build_rl_key,
        build_scope_descriptors,
        build_tpm_key_group,
    )

    run = uuid.uuid4().hex[:8]
    model_a = f"glob-a-{run}"
    # 두 GLOBAL 키가 **다른 슬롯**에 있어야 아래 대조군(CROSSSLOT)이 의미가 있다.
    slot_a = cluster.keyslot(build_rl_key(RateLimitScope.GLOBAL, None, model_a, "rpm"))
    model_b = next(
        m
        for m in (f"glob-b{i}-{run}" for i in range(50))
        if cluster.keyslot(build_rl_key(RateLimitScope.GLOBAL, None, m, "rpm")) != slot_a
    )
    keys = [build_rl_key(RateLimitScope.GLOBAL, None, m, "rpm") for m in (model_a, model_b)]
    for m in (model_a, model_b):
        keys += build_tpm_key_group(RateLimitScope.GLOBAL, None, m)

    def _desc(model, rpm, tpm):
        return build_scope_descriptors(
            user_id=f"itg-{run}", team_id=None, model_alias=model,
            user_rpm=None, user_tpm=None, team_rpm=None, team_tpm=None,
            global_rpm=rpm, global_tpm=tpm,
        )

    spy = _FailOpenSpy()
    rls.set_fail_open_metric(spy)
    try:
        svc = rls.RateLimitService()
        desc_a, desc_b = _desc(model_a, 2, 100), _desc(model_b, 3, 1000)
        seq_a = [(await svc.check_multi_scope_rpm(cluster, desc_a)).allowed for _ in range(3)]
        seq_b = [(await svc.check_multi_scope_rpm(cluster, desc_b)).allowed for _ in range(4)]
        assert seq_a == [True, True, False]
        assert seq_b == [True, True, True, False]  # a 의 소진과 무관하다
        assert (await svc.check_multi_scope_rpm(cluster, desc_a)).scope == "GLOBAL"

        assert (await svc.check_multi_scope_tpm(cluster, desc_a, reserved_tokens=60)).allowed
        assert not (await svc.check_multi_scope_tpm(cluster, desc_a, reserved_tokens=60)).allowed
        assert (await svc.check_multi_scope_tpm(cluster, desc_b, reserved_tokens=60)).allowed
        assert spy.count == 0  # 무음 미집행 없음

        # 대조군 — 두 모델의 GLOBAL 키를 한 eval 에 묶으면 클러스터가 거부한다. 키가 다른
        # 노드에 있으면 클라이언트가 보내기 전에(RedisClusterException), 같은 노드면 서버가
        # (CROSSSLOT) 거부한다 — 어느 쪽이든 게이트웨이의 except 가 삼켜 fail-open 이 된다.
        with pytest.raises((RedisError, RedisClusterException)) as crossslot:
            await cluster.eval(
                LuaScriptLoader.get("rate_limit_check"), 2, keys[0], keys[1],
                "0", "req", "60000", "2", "5", "5", "GLOBAL", "GLOBAL",
            )
        msg = str(crossslot.value)
        assert "CROSSSLOT" in msg.upper() or "same slot" in msg or "same key slot" in msg
    finally:
        rls.set_fail_open_metric(None)
        for key in keys:
            await cluster.delete(key)
        await cluster.aclose()

