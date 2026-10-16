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
  4) 응답이 늦게 와 운영 설정 클라이언트(socket_timeout 2초, redis_retries 1)가 같은 EVAL 을 다시
     보내도 예약(TPM·비용), 정산 파이프라인, 월 예산 차감(finalize 의 budget_deduct)은 한 번만
     반영되고, RPM 은 받아들인 요청을 429 로 바꾸지 않는다 — TCP 프록시로 응답을 잡아 두어
     재현한다(단일 노드·클러스터).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest

CLUSTER_URL = os.environ.get("REDIS_CLUSTER_URL")
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

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


# ─────────────────────────────────────────────────────────────────────────────
# 늦은 응답 + 클라이언트 재시도 — 같은 EVAL 이 두 번 가도 한 번만 반영된다
# ─────────────────────────────────────────────────────────────────────────────


class _LateReplyProxy:
    """Redis 노드 하나 앞의 TCP 프록시.

    ``arm(trigger)`` 뒤 trigger 바이트를 담은 **첫** 요청은 그대로 서버로 간다 — 서버는 실행한다. 그
    응답만 클라이언트에 주지 않고 잡아 둔다(클라이언트가 socket_timeout 으로 연결을 끊을 때까지).
    그러면 redis-py 가 새 연결로 같은 명령을 다시 보내고, 그 요청은 그대로 통과한다. 늦게 온 응답과
    재시도를 네트워크에서 재현한다 — 클라이언트 코드는 건드리지 않는다.
    """

    def __init__(self, host: str, port: int) -> None:
        self.upstream = (host, port)
        self.trigger: bytes | None = None
        self.seen = 0  # trigger 를 담은 요청 수
        self.held = 0  # 응답을 잡아 둔 횟수
        self.port = 0
        self._server = None
        self._conns: set = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        # 남은 연결을 먼저 끊는다 — Python 3.12 의 wait_closed 는 모든 연결이 끝날 때까지 기다리고,
        # redis-py 가 재초기화 중에 연 연결 하나가 남아 있으면 영원히 기다린다(실측).
        self._server.close()
        for c_writer, u_writer in list(self._conns):
            c_writer.close()
            u_writer.close()
        await asyncio.wait_for(self._server.wait_closed(), 5)

    def arm(self, trigger: bytes) -> None:
        self.trigger, self.seen, self.held = trigger, 0, 0

    async def _serve(self, c_reader, c_writer) -> None:
        try:
            u_reader, u_writer = await asyncio.open_connection(*self.upstream)
        except OSError:
            c_writer.close()
            return
        conn = (c_writer, u_writer)
        self._conns.add(conn)
        hold, gone = asyncio.Event(), asyncio.Event()

        async def up() -> None:
            try:
                while data := await c_reader.read(65536):
                    if self.trigger and self.trigger in data:
                        self.seen += 1
                        if not self.held:
                            self.held += 1
                            hold.set()
                    u_writer.write(data)
                    await u_writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                gone.set()
                u_writer.close()

        async def down() -> None:
            try:
                while data := await u_reader.read(65536):
                    if hold.is_set():
                        await gone.wait()  # 응답을 주지 않는다 — 클라이언트가 끊을 때까지
                        return
                    c_writer.write(data)
                    await c_writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                c_writer.close()

        try:
            await asyncio.gather(up(), down())
        finally:
            self._conns.discard(conn)


@pytest.fixture(params=["single", "cluster"])
async def late(request):
    """운영 설정 그대로의 클라이언트(app.redis_client 의 kwargs: socket_timeout 2초, connect 1초,
    redis_retries 1)가 프록시를 거쳐 Redis 에 닿는다. 클러스터는 노드마다 프록시를 두고
    ``address_remap`` 으로 그리로 보낸다.

    반환: (클라이언트, 프록시들, 프록시를 거치지 않는 클라이언트).
    """
    from redis.asyncio import Redis
    from redis.asyncio.cluster import RedisCluster

    import app
    from app.config import Settings
    from app.redis_client import _cluster_kwargs, _resilience_kwargs
    from app.services.lua_loader import LuaScriptLoader

    # 시험하는 패키지의 스크립트를 쓴다(대조군 트리를 pythonpath 로 돌릴 때 그 트리의 스크립트).
    LuaScriptLoader.load_all(Path(app.__file__).resolve().parent / "redis_scripts")
    settings = Settings()
    proxies: list[_LateReplyProxy] = []
    if request.param == "single":
        if not REDIS_PROOF_URL:
            pytest.skip("REDIS_PROOF_URL 미설정 — 실 Redis 필요")
        u = urlparse(REDIS_PROOF_URL)
        direct = Redis.from_url(REDIS_PROOF_URL)
        proxy = _LateReplyProxy(u.hostname, u.port or 6379)
        await proxy.start()
        proxies.append(proxy)
        db = int((u.path or "/0").lstrip("/") or 0)
        client = Redis(host="127.0.0.1", port=proxy.port, db=db, **_resilience_kwargs(settings))
    else:
        direct = RedisCluster.from_url(CLUSTER_URL)
        await direct.initialize()
        remap: dict[tuple[str, int], tuple[str, int]] = {}
        for node in direct.get_nodes():
            proxy = _LateReplyProxy(node.host, node.port)
            await proxy.start()
            proxies.append(proxy)
            remap[(node.host, node.port)] = ("127.0.0.1", proxy.port)
        first = direct.get_nodes()[0]
        client = RedisCluster(
            host="127.0.0.1",
            port=remap[(first.host, first.port)][1],
            address_remap=lambda hp: remap.get(hp, hp),
            **_cluster_kwargs(settings),
        )
        # 슬롯 맵을 먼저 받아 둔다 — 첫 명령에서 늦게 초기화하면 그 명령이 다른 노드의 프록시에 먼저
        # 닿아, 응답을 잡아 두는 자리가 두 군데가 될 수 있다(실측: 잡아 둔 응답 2).
        await client.initialize()
    try:
        yield client, proxies, direct
    finally:
        await client.aclose()
        await direct.aclose()
        for proxy in proxies:
            await proxy.close()


def _arm(proxies, trigger: str) -> None:
    for proxy in proxies:
        proxy.arm(trigger.encode())


def _retried(proxies) -> tuple[int, int]:
    """(잡아 둔 응답 수, trigger 를 담은 요청 수) — (1, 2) 여야 재시도가 실제로 일어난 것이다."""
    return sum(p.held for p in proxies), sum(p.seen for p in proxies)


async def _drop(direct, *patterns: str) -> None:
    for pattern in patterns:
        async for raw in direct.scan_iter(match=pattern, count=500):
            await direct.delete(raw)


@pytest.mark.asyncio
async def test_a_late_tpm_reservation_reply_and_its_retry_reserve_once(late):
    """TPM 예약 EVAL 의 응답이 늦게 와 클라이언트가 다시 보낸다 → cur 는 예약 한 번(500)이다.
    예전에는 두 번 실행돼 1000 이었다 — 정산은 한 번만 되돌리므로 남은 500 이 버킷이 지날 때까지
    남는다."""
    from app.services.rate_limit_scope import (
        RateLimitScope,
        build_scope_descriptors,
        build_tpm_key_group,
    )
    from app.services.rate_limit_service import RateLimitService

    client, proxies, direct = late
    uid = f"lr-t-{uuid.uuid4().hex[:8]}"
    desc = build_scope_descriptors(
        user_id=uid, team_id=None, model_alias="m", user_rpm=None, user_tpm=100_000,
        team_rpm=None, team_tpm=None, global_rpm=None, global_tpm=None,
    )
    cur, _prev, _win = build_tpm_key_group(RateLimitScope.USER, uid, "m")
    _arm(proxies, cur)
    try:
        res = await RateLimitService().check_multi_scope_tpm(
            client, desc, reserved_tokens=500, reservation_id=uuid.uuid4().hex
        )
        assert _retried(proxies) == (1, 2), "응답을 잡아 두고 redis-py 가 다시 보냈어야 한다"
        assert res.allowed and len(res.reserved_keys) == 1
        assert int(await direct.get(cur)) == 500, "같은 예약이 두 번 반영됐다"
    finally:
        await _drop(direct, f"{{USER:{uid}:*}}:*")


@pytest.mark.asyncio
async def test_a_late_cost_reservation_reply_and_its_retry_reserve_once(late):
    """비용 예약 EVAL 의 응답이 늦게 와 클라이언트가 다시 보낸다 → cpm·cph 는 예약 한 번(0.5)이다.
    예전에는 1.0 이었다 — cph 에서는 그 여분이 최대 한 시간 남의 한도처럼 남는다."""
    from app.services.rate_limit_service import RateLimitService

    client, proxies, direct = late
    uid = f"lr-c-{uuid.uuid4().hex[:8]}"
    _arm(proxies, f"rl:cost:user:{{{uid}}}:cpm")
    try:
        res = await RateLimitService().reserve_cost(
            client, user_id=uid, estimated_cost=Decimal("0.5"),
            user_cpm_limit=Decimal("100"), user_cph_limit=Decimal("1000"),
            reservation_id=uuid.uuid4().hex,
        )
        assert _retried(proxies) == (1, 2), "응답을 잡아 두고 redis-py 가 다시 보냈어야 한다"
        assert res.allowed and res.reserved_cost == Decimal("0.5")
        (cpm, cph), = res.reserved_keys
        assert (float(await direct.get(cpm)), float(await direct.get(cph))) == (0.5, 0.5), (
            "같은 예약이 두 번 반영됐다"
        )
    finally:
        await _drop(direct, f"rl:cost:user:{{{uid}}}:*", f"rl:cost-*:user:{{{uid}}}:*")


@pytest.mark.asyncio
async def test_a_late_rpm_reply_and_its_retry_do_not_turn_the_request_into_a_429(late):
    """RPM 한도 1, 첫 요청의 EVAL 응답이 늦게 와 다시 보낸다. 멤버는 요청 id 라 두 번 실행돼도
    ZCARD 는 1 이다(이미 멱등) — 그런데 다시 보낸 EVAL 은 그 멤버까지 세어 한도에 닿았다고 보고
    거부를 돌려줬다. 받아들여 한 칸을 쓴 요청이 429 를 받는다. 이제 이미 들어 있는 멤버면 검사를
    건너뛰고 처음 결과(통과)를 돌려준다."""
    from app.services.rate_limit_scope import RateLimitScope, build_rl_key, build_scope_descriptors
    from app.services.rate_limit_service import RateLimitService

    client, proxies, direct = late
    uid = f"lr-r-{uuid.uuid4().hex[:8]}"
    desc = build_scope_descriptors(
        user_id=uid, team_id=None, model_alias="m", user_rpm=1, user_tpm=None,
        team_rpm=None, team_tpm=None, global_rpm=None, global_tpm=None,
    )
    key = build_rl_key(RateLimitScope.USER, uid, "m", "rpm")
    _arm(proxies, key)
    try:
        res = await RateLimitService().check_multi_scope_rpm(
            client, desc, request_id=f"req-{uuid.uuid4().hex[:8]}"
        )
        assert _retried(proxies) == (1, 2), "응답을 잡아 두고 redis-py 가 다시 보냈어야 한다"
        assert await direct.zcard(key) == 1
        assert res.allowed, "받아들여 한 칸을 쓴 요청이 다시 보낸 EVAL 때문에 429 가 됐다"
    finally:
        await direct.delete(key)


@pytest.mark.asyncio
async def test_a_late_budget_deduct_reply_and_its_retry_charge_the_budget_once(late):
    """finalize 의 사용자 예산 차감 EVAL(budget_deduct) 응답이 늦게 와 클라이언트가 다시 보낸다 →
    월 예산 카운터에는 그 요청의 비용이 한 번 들어간다. 예전에는 두 번 들어가 집행이 예산을 일찍
    소진했다(DB 원장은 원장대로 맞다). 같은 요청의 팀 차감(재시도 없음)도 한 번이다."""
    from app.periods import current_kst_period
    from app.schemas.domain import (
        ApiFormat,
        AuthContext,
        AuthType,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
        Role,
        TokenUsage,
    )
    from app.services.cost_recorder import COST_STREAM_KEY, CostRecorder, calculate_cost

    client, proxies, direct = late
    run = uuid.uuid4().hex[:8]
    auth = AuthContext(
        user_id=f"lr-b-{run}", team_id=f"lr-bt-{run}", dept_id=str(uuid.uuid4()),
        roles=[Role.USER], auth_type=AuthType.VIRTUAL_KEY,
    )
    model = ModelConfigSchema(
        provider_model_id="anthropic.claude-sonnet-4-6", alias=f"lr-bm-{run}",
        provider=ProviderType.BEDROCK, api_format=ApiFormat.ANTHROPIC_MESSAGES,
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        status=ModelStatus.ACTIVE,
    )
    usage = TokenUsage(input_tokens=1000, output_tokens=500, total_tokens=1500)
    cost = float(calculate_cost(usage, model))
    period = current_kst_period()
    user_key = f"budget:user:{{{auth.user_id}}}:{period}"
    team_key = f"budget:team:{{{auth.team_id}}}:{period}"
    _arm(proxies, user_key)
    try:
        await CostRecorder().finalize(client, auth, model, usage, f"req-{run}", False, 5)
        assert _retried(proxies) == (1, 2), "응답을 잡아 두고 redis-py 가 다시 보냈어야 한다"
        charged = (float(await direct.get(user_key)), float(await direct.get(team_key)))
        assert charged == pytest.approx((cost, cost)), f"예산이 두 번 차감됐다: {charged}"
    finally:
        await _drop(
            direct,
            f"budget:*{{{auth.user_id}}}*", f"budget:*{{{auth.team_id}}}*",
            f"budget-deducted:*{{{auth.user_id}}}*", f"budget-deducted:*{{{auth.team_id}}}*",
        )
        own = [
            entry_id
            for entry_id, fields in await direct.xrange(COST_STREAM_KEY, "-", "+")
            if auth.user_id.encode() in fields.get(b"payload", b"")
        ]
        if own:
            await direct.xdel(COST_STREAM_KEY, *own)


async def _settle_late(late, kind: str):
    """예약은 프록시를 거치지 않는 클라이언트로, **정산**만 운영 설정 클라이언트로 — 정산
    파이프라인의 EVAL 응답을 한 번 잡아 둔다. 예약 500·0.5 → 실제 200·0.2.

    반환: (사용자 id, 정산 뒤 그 키의 값, (잡아 둔 응답 수, 본 요청 수)).
    """
    from app.services.rate_limit_scope import build_scope_descriptors
    from app.services.rate_limit_service import RateLimitService

    client, proxies, direct = late
    svc = RateLimitService()
    uid, rid = f"ls-{kind[0]}-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    if kind == "tpm":
        desc = build_scope_descriptors(
            user_id=uid, team_id=None, model_alias="m", user_rpm=None, user_tpm=100_000,
            team_rpm=None, team_tpm=None, global_rpm=None, global_tpm=None,
        )
        res = await svc.check_multi_scope_tpm(direct, desc, 500, reservation_id=rid)
        key = res.reserved_keys[0][0]
        _arm(proxies, key.replace(":tpm:cur", ":tpm-settled:"))
        await svc.settle_tpm(
            client, desc, 500, 200, reserved_keys=res.reserved_keys, reservation_id=rid
        )
        value = int(await direct.get(key))
    else:
        res = await svc.reserve_cost(
            direct, user_id=uid, estimated_cost=Decimal("0.5"),
            user_cpm_limit=Decimal("100"), user_cph_limit=Decimal("1000"), reservation_id=rid,
        )
        key = res.reserved_keys[0][0]
        _arm(proxies, f"rl:cost-settled:user:{{{uid}}}:")
        await svc.settle_cost(
            client, user_id=uid, actual_cost=Decimal("0.2"), reserved_cost=Decimal("0.5"),
            reserved_keys=res.reserved_keys, reservation_id=rid,
        )
        value = float(await direct.get(key))
    return uid, value, _retried(proxies)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tpm", "cost"])
async def test_a_late_settle_pipeline_reply_settles_once(late, kind):
    """정산 파이프라인(``pipeline(transaction=False)``)의 EVAL 응답이 늦게 온다.

    * 단일 노드: redis-py 의 ``Pipeline.execute`` 가 운영 설정의 재시도로 파이프라인을 **다시
      보낸다**(잡아 둔 1, 본 2). 정산 표지가 두 번째를 막아 한 번만 반영된다 — 표지를 빼면 두 번
      환불돼 0 하한에 걸린다(대조군).
    * 클러스터: ``ClusterPipeline`` 은 ``raise_on_error=False`` 의 명령별 TimeoutError 를 결과로
      돌려줄 뿐 다시 보내지 않는다(잡아 둔 1, 본 1, ``*_settle_failed`` 경고) — 서버는 한 번
      실행했다. redis-py 가 이것을 바꾸면 이 단정이 먼저 알린다. 클러스터에서 표지가 막는 두 번째
      정산은 test_medium_*_settle 의 "second settle" 테스트가 본다.
    """
    from redis.asyncio.cluster import RedisCluster

    client, _proxies, direct = late
    uid, value, retried = await _settle_late(late, kind)
    try:
        assert retried == ((1, 1) if isinstance(client, RedisCluster) else (1, 2)), retried
        assert value == (200 if kind == "tpm" else pytest.approx(0.2)), "정산이 두 번 반영됐다"
    finally:
        await _drop(direct, f"{{USER:{uid}:*}}:*", f"rl:cost*:user:{{{uid}}}:*")
