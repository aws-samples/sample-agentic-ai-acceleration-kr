# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""뒤 스코프가 거부한 요청은 앞 스코프에 넣은 TPM·비용 예약을 **정확히 한 번** 되돌린다.

무엇이 문제였나
---------------
``enforce_rate_limits`` 는 RPM → TPM → 비용 순서로, 각 단계 안에서는 USER → TEAM → GLOBAL 순서로
검사하고 통과한 스코프에는 그 자리에서 예약을 넣는다. 뒤 스코프가 거부하면 요청은 429 로 끝나고
정산 상태(``rate_limit_state``)를 받지 않으므로 아무도 그 예약을 되돌리지 않았다.

  * 비용: USER 통과·TEAM 거부 → USER 의 cpm·cph 예약이 창 끝까지(cpm 1분, **cph 최대 1시간**)
    남는다.
  * TPM: USER 통과·TEAM(또는 GLOBAL) 거부 → 앞 스코프의 예약이 버킷이 넘어갈 때까지 남는다.
  * 비용 단계의 거부 → 그 앞의 TPM 단계가 세 스코프에 넣은 예약이 모두 남는다.

남은 예약은 그 사용자의 다음 요청들을 실제로 쓰지 않은 양만큼 막는다. 팀이 한도에 걸려 있는 동안
그 팀원의 요청은 매번 USER 예약을 남기므로, 팀이 풀린 뒤에도 USER 한도에서 429 를 맞는다.

이 파일
-------
실 Redis 와 실 Lua, 예약·거부는 ``enforce_rate_limits`` 그대로(한도 로더만 대역). 거부된 요청
뒤에 USER(·TEAM) 카운터가 그 요청 전의 값과 **정확히** 같은지, 앞서 통과한(정산 전) 요청의 예약은
그대로인지, 그리고 ``rl:cost:*`` · ``{*}:tpm:*`` 어디에도 TTL −1 키나 음수가 없는지를 단정한다.
모든 시나리오가 단일 노드(``REDIS_PROOF_URL``)와 진짜 Redis Cluster(``REDIS_CLUSTER_URL``)에서
한 번씩 돈다 — USER 와 TEAM 은 hash tag 가 달라 클러스터에서 서로 다른 슬롯이다. env 가 없으면 그
변형은 skip 한다.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

import app
from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    Role,
)
from app.services.lua_loader import LuaScriptLoader
from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits
from app.services.rate_limit_enforcement import enforce_rate_limits
from app.services.rate_limit_scope import RateLimitScope, build_tpm_key_group

#: 테스트하는 패키지 자신의 Lua(대조군을 다른 트리로 돌리면 그 트리의 스크립트를 쓴다).
_SCRIPTS = Path(app.__file__).resolve().parent / "redis_scripts"
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")
REDIS_CLUSTER_URL = os.environ.get("REDIS_CLUSTER_URL")

#: 실 Redis 클라이언트 설정. ⚠️ 재시도를 끈다 — redis-py 는 응답이 socket_timeout 뒤에 온 **직접**
#: EVAL(예약 검사)을 다시 보내 서버에서 두 번 실행한다(실측: INCRBY 100 한 번이 200 이 됐다).
#: 그러면 부하가 큰 머신에서 카운터가 조용히 틀려 로직 결함처럼 보인다(load average 150–300 에서
#: ``…leaves_the_actual_tokens[cluster-partial]`` 가 한 번 실패한 것). 늦은 응답은 오류로 드러나야
#: 한다. 30초는 그런 부하에서도 거의 넘지 않는다.
_CLIENT_KW = {"socket_timeout": 30, "retry": Retry(NoBackoff(), 0)}


def _client(kind: str):
    if kind == "single":
        if not REDIS_PROOF_URL:
            pytest.skip(
                "REDIS_PROOF_URL 미설정 — 실 Redis 필요. 되돌리기는 INCRBY/INCRBYFLOAT 로 카운터를 "
                "움직이는 것이라, 호출 여부만 보는 가짜로는 남은 예약도 음수도 보이지 않는다."
            )
        import redis.asyncio as aioredis

        return aioredis.from_url(REDIS_PROOF_URL, **_CLIENT_KW)
    if not REDIS_CLUSTER_URL:
        pytest.skip(
            "REDIS_CLUSTER_URL 미설정 — 진짜 Redis Cluster 필요(단일 노드는 CROSSSLOT 을 "
            "일으키지 않는다, 가짜/단일노드 폴백 금지)."
        )
    from redis.asyncio.cluster import RedisCluster

    return RedisCluster.from_url(REDIS_CLUSTER_URL, **_CLIENT_KW)


@pytest.fixture(params=["single", "cluster"])
async def real_redis(request):
    client = _client(request.param)
    LuaScriptLoader.load_all(_SCRIPTS)
    try:
        yield client
    finally:
        await client.aclose()


class _Case:
    def __init__(self) -> None:
        run = uuid.uuid4().hex[:10]
        self.auth = AuthContext(
            user_id=f"rd-u-{run}",
            team_id=f"rd-t-{run}",
            dept_id=str(uuid.uuid4()),
            roles=[Role.USER],
            auth_type=AuthType.VIRTUAL_KEY,
        )
        self.model = ModelConfigSchema(
            provider_model_id="anthropic.claude-sonnet-4-6",
            alias=f"rd-m-{run}",
            provider=ProviderType.BEDROCK,
            api_format=ApiFormat.ANTHROPIC_MESSAGES,
            pricing=ModelPricingSchema(
                input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")
            ),
            status=ModelStatus.ACTIVE,
        )
        self.tpm = {
            "USER": build_tpm_key_group(RateLimitScope.USER, self.auth.user_id, self.model.alias),
            "TEAM": build_tpm_key_group(RateLimitScope.TEAM, self.auth.team_id, self.model.alias),
            "GLOBAL": build_tpm_key_group(RateLimitScope.GLOBAL, None, self.model.alias),
        }


_OPEN = {"tpm": 10_000_000, "cpm": Decimal("100"), "cph": Decimal("1000")}


def _limits(user=None, team=None, global_=None):
    """스코프마다 {"tpm", "cpm", "cph"} — 빠진 칸은 한도 없음. RPM 은 걸지 않는다."""

    def _scope(d) -> ScopeLimits:
        d = d or {}
        return ScopeLimits(tpm=d.get("tpm"), cpm=d.get("cpm"), cph=d.get("cph"))

    async def _load(**_kwargs):
        return AllScopeLimits(user=_scope(user), team=_scope(team), global_=_scope(global_))

    return _load


async def _enforce(monkeypatch, r, case: _Case, limits):
    monkeypatch.setattr("app.services.rate_limit_enforcement.load_all_scope_limits", limits)
    state: dict = {}
    rejected = await enforce_rate_limits(
        redis=r,
        auth_context=case.auth,
        model_config=case.model,
        body={
            "model": case.model.alias,
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": "release me"}],
        },
        state=state,
        request_id=f"req-{uuid.uuid4().hex[:8]}",
    )
    return state, rejected


def _text(raw) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def _keyspace(r, case: _Case) -> dict[str, tuple[float, int]]:
    """이 시나리오의 ``rl:cost:*`` 와 ``{*}:tpm:*`` — {키: (값, TTL)}. 클러스터면 모든 primary.

    SCAN 패턴을 시나리오의 id·모델 alias 로 좁힌다 — 같은 PROOF 실행의 다른 테스트가 남긴 키가
    "TTL 없는 키·음수 없음" 단정을 오염시키지 않게.
    """
    out: dict[str, tuple[float, int]] = {}
    uid, tid, alias = case.auth.user_id, case.auth.team_id, case.model.alias
    for pattern in (
        f"rl:cost:user:{{{uid}}}:*",
        f"rl:cost:team:{{{tid}}}:*",
        f"{{USER:{uid}:*}}:tpm:*",
        f"{{TEAM:{tid}:*}}:tpm:*",
        f"{{GLOBAL:*:{alias}}}:tpm:*",
    ):
        async for raw_key in r.scan_iter(match=pattern, count=500):
            key = _text(raw_key)
            raw = await r.get(key)
            if raw is None:
                continue
            out[key] = (float(_text(raw)), int(await r.ttl(key)))
    return out


def _assert_clean(keyspace: dict) -> None:
    ttl_less = {k: v for k, v in keyspace.items() if v[1] == -1}
    negative = {k: v for k, v in keyspace.items() if v[0] < 0}
    assert not ttl_less, f"TTL 없는 rate-limit 키: {ttl_less}"
    assert not negative, f"음수 카운터 — 남의 한도를 빌려 준다: {negative}"


def _cost(keyspace: dict, scope: str, sid: str) -> dict[str, float]:
    """{"cpm"/"cph": 값} — 그 스코프의 비용 키."""
    prefix = f"rl:cost:{scope}:{{{sid}}}:"
    return {k.split(":")[4]: v[0] for k, v in keyspace.items() if k.startswith(prefix)}


def _tpm_cur(keyspace: dict, case: _Case, scope: str) -> float | None:
    v = keyspace.get(case.tpm[scope][0])
    return None if v is None else v[0]


def _body(rejected) -> dict:
    return json.loads(rejected.body)


async def _cleanup(r, case: _Case) -> None:
    for keys in case.tpm.values():
        for key in keys:
            await r.delete(key)
    for pattern in (
        f"rl:cost:user:{{{case.auth.user_id}}}:*",
        f"rl:cost:team:{{{case.auth.team_id}}}:*",
    ):
        async for raw_key in r.scan_iter(match=pattern, count=500):
            await r.delete(raw_key)


async def _clear_of_window_edge() -> None:
    """비용 키는 분 이름이다 — 한 시나리오의 두 요청이 같은 분에 떨어지게 분 끝 2초는 피한다."""
    while time.time() % 60 > 58:
        await asyncio.sleep(0.2)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 비용 — TEAM 이 거부하면 USER 예약을 되돌린다
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_team_cost_denial_undoes_only_this_requests_user_reservation(
    real_redis, monkeypatch
):
    """먼저 통과한 요청 A(정산 전)의 예약은 남고, TEAM 에서 거부된 요청 B 가 USER 에 넣은 예약만
    빠진다 — USER cpm·cph = A 의 예약 그대로. TEAM 은 B 를 거부했으니 B 의 TEAM 예약은 없다."""
    r, case = real_redis, _Case()
    open_cost = {"cpm": _OPEN["cpm"], "cph": _OPEN["cph"]}
    await _clear_of_window_edge()
    try:
        state_a, rejected = await _enforce(monkeypatch, r, case, _limits(open_cost, open_cost))
        assert rejected is None
        reserved_a = float(state_a["rate_limit_state"]["cost_reserved"])

        _s, rejected = await _enforce(
            monkeypatch, r, case, _limits(open_cost, {"cpm": Decimal("0.02"), "cph": Decimal("1")})
        )
        assert rejected is not None and rejected.status_code == 429
        assert _body(rejected)["error"]["code"].startswith("team_")

        keyspace = await _keyspace(r, case)
        _assert_clean(keyspace)
        assert _cost(keyspace, "user", case.auth.user_id) == {"cpm": reserved_a, "cph": reserved_a}
        assert _cost(keyspace, "team", case.auth.team_id) == {"cpm": reserved_a, "cph": reserved_a}
    finally:
        await _cleanup(r, case)


async def test_a_cost_denial_also_undoes_the_tpm_reservations(real_redis, monkeypatch):
    """비용 단계(TEAM)의 거부 → 그 앞 TPM 단계가 USER·TEAM·GLOBAL 에 넣은 예약도 모두 0 이 된다."""
    r, case = real_redis, _Case()
    await _clear_of_window_edge()
    try:
        _s, rejected = await _enforce(
            monkeypatch,
            r,
            case,
            _limits(
                {"tpm": _OPEN["tpm"], "cpm": _OPEN["cpm"]},
                {"tpm": _OPEN["tpm"], "cpm": Decimal("0.0001")},
                {"tpm": _OPEN["tpm"]},
            ),
        )
        assert rejected is not None and rejected.status_code == 429

        keyspace = await _keyspace(r, case)
        _assert_clean(keyspace)
        assert {s: _tpm_cur(keyspace, case, s) for s in case.tpm} == {
            "USER": 0, "TEAM": 0, "GLOBAL": 0
        }
        assert _cost(keyspace, "user", case.auth.user_id) == {"cpm": 0.0, "cph": 0.0}
        assert _cost(keyspace, "team", case.auth.team_id) == {}
    finally:
        await _cleanup(r, case)


# ─────────────────────────────────────────────────────────────────────────────
# 2. TPM — 뒤 스코프가 거부하면 앞 스코프 예약을 되돌린다
# ─────────────────────────────────────────────────────────────────────────────


async def test_a_team_tpm_denial_undoes_only_this_requests_user_reservation(
    real_redis, monkeypatch
):
    """통과한 요청 A 의 예약은 남고, TEAM TPM 에서 거부된 B 가 USER 에 넣은 예약만 빠진다."""
    r, case = real_redis, _Case()
    try:
        state_a, rejected = await _enforce(
            monkeypatch, r, case, _limits({"tpm": _OPEN["tpm"]}, {"tpm": _OPEN["tpm"]})
        )
        assert rejected is None
        reserved_a = state_a["rate_limit_state"]["tpm_reserved"]

        _s, rejected = await _enforce(
            monkeypatch, r, case, _limits({"tpm": _OPEN["tpm"]}, {"tpm": reserved_a + 10})
        )
        assert rejected is not None and rejected.status_code == 429
        assert _body(rejected)["error"]["code"] == "team_tpm_exceeded"

        keyspace = await _keyspace(r, case)
        _assert_clean(keyspace)
        assert _tpm_cur(keyspace, case, "USER") == reserved_a
        assert _tpm_cur(keyspace, case, "TEAM") == reserved_a
    finally:
        await _cleanup(r, case)


async def test_a_global_tpm_denial_undoes_the_user_and_team_reservations(
    real_redis, monkeypatch
):
    """GLOBAL 이 거부 → 이 요청이 USER 와 TEAM 에 넣은 예약이 둘 다 0 으로 돌아간다."""
    r, case = real_redis, _Case()
    try:
        _s, rejected = await _enforce(
            monkeypatch,
            r,
            case,
            _limits({"tpm": _OPEN["tpm"]}, {"tpm": _OPEN["tpm"]}, {"tpm": 10}),
        )
        assert rejected is not None and rejected.status_code == 429
        keyspace = await _keyspace(r, case)
        _assert_clean(keyspace)
        assert {s: _tpm_cur(keyspace, case, s) for s in case.tpm} == {
            "USER": 0, "TEAM": 0, "GLOBAL": 0
        }
    finally:
        await _cleanup(r, case)


# ─────────────────────────────────────────────────────────────────────────────
# 3. fail-closed — TEAM 의 eval 이 실패해 막아도 USER 예약을 되돌린다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stage", ["tpm", "cost"])
async def test_a_fail_closed_team_check_undoes_the_user_reservation(
    real_redis, monkeypatch, stage
):
    """rl_fail_mode=closed 에서 TEAM 샤드가 실패하면 요청이 막힌다 — USER 예약도 되돌린다."""
    from app.services import rate_limit_service as rls

    r, case = real_redis, _Case()
    team_keys = {case.tpm["TEAM"][0], f"rl:cost:team:{{{case.auth.team_id}}}:"}
    real_eval = rls._guarded_eval

    async def _team_shard_down(redis, *args):
        if len(args) > 2 and any(str(args[2]).startswith(k) for k in team_keys):
            raise ConnectionError("TEAM shard down")
        return await real_eval(redis, *args)

    monkeypatch.setattr(rls, "_guarded_eval", _team_shard_down)
    monkeypatch.setattr(rls, "_fail_closed", lambda: True)
    limits = (
        _limits({"tpm": _OPEN["tpm"]}, {"tpm": _OPEN["tpm"]})
        if stage == "tpm"
        else _limits({"cpm": _OPEN["cpm"]}, {"cpm": _OPEN["cpm"]})
    )
    await _clear_of_window_edge()
    try:
        _s, rejected = await _enforce(monkeypatch, r, case, limits)
        assert rejected is not None and rejected.status_code == 429
        keyspace = await _keyspace(r, case)
        _assert_clean(keyspace)
        if stage == "tpm":
            assert _tpm_cur(keyspace, case, "USER") == 0
            assert _tpm_cur(keyspace, case, "TEAM") is None
        else:
            assert _cost(keyspace, "user", case.auth.user_id) == {"cpm": 0.0, "cph": 0.0}
            assert _cost(keyspace, "team", case.auth.team_id) == {}
    finally:
        await _cleanup(r, case)
