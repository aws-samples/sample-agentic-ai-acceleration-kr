# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""거부된 요청이 **앞 단계에서 잡은 예약을 물고 있지 않은지**, 그리고 Codex 가 강등을 받는지.

두 결함
-------
1. **다중 스코프/다중 단계 예약이 되돌려지지 않았다.** 레이트리밋은 세 단계를 순서대로
   지나고, 각 단계는 스코프를 fast-fail 순서(USER → TEAM → GLOBAL)로 본다:

       RPM   ZSET 에 이 요청의 멤버를 넣는다
       TPM   창(60s)에 예약분을 미리 깎는다
       비용  CPM/CPH 창에 추정 비용을 미리 깎는다

   어느 단계·어느 스코프에서 거부되든, **그 앞에서 이미 쓴 것**은 그대로 남았다. 구체적으로:

     * USER RPM 통과 → TEAM RPM 거부 ⇒ 서빙되지 않은 요청이 사용자의 RPM 창을 차지한다.
     * USER TPM 통과 → TEAM TPM 거부 ⇒ 사용자의 TPM 창이 예약분만큼 물린다.
     * RPM/TPM 통과 → 비용 거부 ⇒ 둘 다 남는다. 게다가 통과 경로에서만 써지는
       ``state["rate_limit_state"]`` 가 없으므로 하류의 ``release_reservations`` 도
       이 요청에 대해 볼 것이 없다 — **아무도** 되돌릴 수 없는 상태다.

   팀 한도에 계속 걸리는 상황에서 사용자 자신의 창이 서빙되지 않은 요청으로 채워진다.

2. **``/v1/responses`` 가 강등 경로 목록에 없었다.** Codex 는 그 경로로만 온다. 그래서
   그 클라이언트의 트래픽은 예산 임계 자동 강등을 통째로 무시했다 — 관리자는 규칙을 저장하고
   화면에서 활성으로 보는데, 정작 비용이 큰 클라이언트에만 적용되지 않는다. 설정이 없는
   것보다 나쁘다: 적용되고 있다고 믿게 된다.

⚠️ 되돌리기는 **실 Redis** 로 확인한다. ZSET 멤버 수와 TPM 카운터 값을 읽어야 "되돌렸다" 를
   증명할 수 있고, 호출 여부만 보면 부호가 반대인 되돌리기도 통과한다.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.services.rate_limit_scope import (
    RateLimitScope,
    ScopeDescriptor,
    build_rl_key,
)
from app.services.rate_limit_service import RateLimitService

REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")


@pytest.fixture(autouse=True)
def _load_lua_scripts():
    """Lua 스크립트는 앱 부팅 시 ``load_all`` 로 읽힌다(main.py).

    ⚠️ 테스트는 그 부팅을 거치지 않으므로 직접 읽어 준다. 안 하면
       ``KeyError: not loaded`` 가 나고, 그 실패는 "되돌리기가 동작하지 않는다" 와
       구별되지 않는다.
    """
    from pathlib import Path

    from app.services.lua_loader import LuaScriptLoader

    LuaScriptLoader.load_all(
        Path(__file__).resolve().parents[2] / "src" / "app" / "redis_scripts"
    )


_redis_required = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason=(
        "REDIS_PROOF_URL 미설정 — 실 Redis 필요. ZSET 멤버 수와 TPM 카운터 값을 읽어야 "
        "'되돌렸다' 가 증명된다(호출 여부만 보면 부호가 반대인 되돌리기도 통과한다)."
    ),
)


def _descriptors(user_id: str, team_id: str, *, rpm=(5, 1), tpm=(1000, 100)):
    """USER 는 넉넉하고 TEAM 은 빡빡한 디스크립터 — fast-fail 로 TEAM 에서 거부된다."""
    return [
        ScopeDescriptor(
            scope=RateLimitScope.USER,
            scope_id=user_id,
            model_alias="proof-model",
            rpm_limit=rpm[0],
            tpm_limit=tpm[0],
        ),
        ScopeDescriptor(
            scope=RateLimitScope.TEAM,
            scope_id=team_id,
            model_alias="proof-model",
            rpm_limit=rpm[1],
            tpm_limit=tpm[1],
        ),
    ]


async def _zcard(redis, d: ScopeDescriptor) -> int:
    return await redis.zcard(build_rl_key(d.scope, d.scope_id, d.model_alias, "rpm"))


async def _tpm_used(redis, d: ScopeDescriptor) -> int:
    """현재 TPM 창의 사용량. 키 구성은 서비스와 같은 빌더로 얻는다."""
    from app.services.rate_limit_scope import build_tpm_key_group

    cur, _prev, _win = build_tpm_key_group(d.scope, d.scope_id, d.model_alias)
    raw = await redis.get(cur)
    return int(raw or 0)


@_redis_required
async def test_rpm_rejection_at_a_later_scope_refunds_the_earlier_one():
    """⚠️ USER 통과 → TEAM 거부. 사용자의 RPM 창에 남으면 안 된다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    user_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    ds = _descriptors(user_id, team_id, rpm=(5, 1))
    svc = RateLimitService()
    try:
        # 팀 한도를 먼저 채운다(다른 사용자의 요청이라고 보면 된다).
        first = await svc.check_multi_scope_rpm(r, ds, request_id=str(uuid.uuid4()))
        assert first.allowed, "셋업 요청이 거부됐다 — 전제가 깨졌다"
        user_after_setup = await _zcard(r, ds[0])

        # 이번 요청은 USER 는 통과하고 TEAM 에서 거부된다.
        rid = str(uuid.uuid4())
        res = await svc.check_multi_scope_rpm(r, ds, request_id=rid)
        assert not res.allowed, "TEAM 한도에 걸리지 않았다 — 전제가 깨졌다"

        user_after = await _zcard(r, ds[0])
        assert user_after == user_after_setup, (
            f"사용자 RPM 창이 {user_after_setup} → {user_after} 로 늘었다 — 서빙되지 않은 "
            "요청이 사용자의 한도를 차지한다"
        )
    finally:
        for d in ds:
            await r.delete(build_rl_key(d.scope, d.scope_id, d.model_alias, "rpm"))
        await r.aclose()


@_redis_required
async def test_tpm_rejection_at_a_later_scope_refunds_the_earlier_one():
    """⚠️ USER TPM 통과 → TEAM TPM 거부. 사용자의 TPM 예약이 남으면 안 된다."""
    import redis.asyncio as aioredis

    from app.services.rate_limit_scope import build_tpm_key_group

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    user_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    # USER 는 여유, TEAM 은 요청 하나도 못 받는 한도.
    ds = _descriptors(user_id, team_id, tpm=(100_000, 10))
    svc = RateLimitService()
    try:
        before = await _tpm_used(r, ds[0])
        res = await svc.check_multi_scope_tpm(r, ds, reserved_tokens=500)
        assert not res.allowed, "TEAM TPM 한도에 걸리지 않았다 — 전제가 깨졌다"
        after = await _tpm_used(r, ds[0])
        assert after == before, (
            f"사용자 TPM 사용량이 {before} → {after} 로 늘었다 — 상류에 가지도 않은 "
            "요청이 예약분을 물고 있다"
        )
    finally:
        for d in ds:
            for k in build_tpm_key_group(d.scope, d.scope_id, d.model_alias):
                await r.delete(k)
        await r.aclose()


@_redis_required
async def test_a_passing_request_still_consumes_both_counters():
    """⚠️ 대조군. 위 단정들이 "항상 되돌린다" 로 통과하는 것이 아님을 보인다.

    이것이 없으면 예약 자체를 없애도 위 두 테스트가 통과한다.
    """
    import redis.asyncio as aioredis

    from app.services.rate_limit_scope import build_tpm_key_group

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    user_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    ds = _descriptors(user_id, team_id, rpm=(5, 5), tpm=(100_000, 100_000))
    svc = RateLimitService()
    try:
        rpm_res = await svc.check_multi_scope_rpm(r, ds, request_id=str(uuid.uuid4()))
        assert rpm_res.allowed
        assert await _zcard(r, ds[0]) == 1, "통과했는데 RPM 카운터가 오르지 않았다"
        assert await _zcard(r, ds[1]) == 1

        tpm_res = await svc.check_multi_scope_tpm(r, ds, reserved_tokens=250)
        assert tpm_res.allowed
        assert await _tpm_used(r, ds[0]) == 250, "통과했는데 TPM 예약이 잡히지 않았다"
        assert await _tpm_used(r, ds[1]) == 250
    finally:
        for d in ds:
            await r.delete(build_rl_key(d.scope, d.scope_id, d.model_alias, "rpm"))
            for k in build_tpm_key_group(d.scope, d.scope_id, d.model_alias):
                await r.delete(k)
        await r.aclose()


@_redis_required
async def test_release_rpm_removes_only_this_request():
    """다른 요청의 멤버까지 지우면 그 사용자들의 한도가 사라진다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    user_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    ds = _descriptors(user_id, team_id, rpm=(10, 10))
    svc = RateLimitService()
    try:
        keep = str(uuid.uuid4())
        drop = str(uuid.uuid4())
        await svc.check_multi_scope_rpm(r, ds, request_id=keep)
        await svc.check_multi_scope_rpm(r, ds, request_id=drop)
        assert await _zcard(r, ds[0]) == 2

        await svc.release_rpm(r, ds, drop)
        assert await _zcard(r, ds[0]) == 1, "다른 요청의 멤버까지 지웠다"
        members = await r.zrange(
            build_rl_key(ds[0].scope, ds[0].scope_id, ds[0].model_alias, "rpm"), 0, -1
        )
        assert any(m.startswith(keep) for m in members), f"남은 멤버: {members}"
    finally:
        for d in ds:
            await r.delete(build_rl_key(d.scope, d.scope_id, d.model_alias, "rpm"))
        await r.aclose()


async def test_release_helpers_never_raise_on_a_dead_redis():
    """되돌리기 실패로 요청 처리를 바꿀 이유가 없다 — 로그만 남고 넘어가야 한다."""

    class _Dead:
        async def zrem(self, *a, **k):
            raise RuntimeError("redis down")

        def pipeline(self, *a, **k):
            raise RuntimeError("redis down")

    ds = _descriptors(str(uuid.uuid4()), str(uuid.uuid4()))
    svc = RateLimitService()
    await svc.release_rpm(_Dead(), ds, "req-1")
    await svc.release_tpm(_Dead(), ds, 100)


# ─────────────────────────────────────────────────────────────────────────────
# 단계 간 되돌리기 배선 + 강등 경로
# ─────────────────────────────────────────────────────────────────────────────


def test_the_cost_rejection_path_releases_both_earlier_stages():
    """⚠️ 이 지점이 유일한 기회다.

    통과 경로에서만 ``state["rate_limit_state"]`` 가 써지므로, 비용 단계에서 거부하면
    하류의 ``release_reservations`` 는 이 요청에 대해 볼 것이 없다.
    """
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "app"
        / "services"
        / "rate_limit_enforcement.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "enforce_rate_limits"
    )
    # 비용 429 를 반환하는 분기 안에서 두 되돌리기가 모두 불려야 한다.
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        returns = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "_build_cost_429"
        ]
        if not returns:
            continue
        called = {
            n.func.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "release_tpm" in called, f"L{node.lineno}: TPM 예약을 되돌리지 않는다"
        assert "release_rpm" in called, f"L{node.lineno}: RPM 카운터를 되돌리지 않는다"
        return
    pytest.fail("비용 429 반환 분기를 찾지 못했다 — 이 검사의 전제가 깨졌다")


def test_the_tpm_rejection_path_releases_rpm():
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "app"
        / "services"
        / "rate_limit_enforcement.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "enforce_rate_limits"
    )
    hits = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if "tpm_result" not in names:
            continue
        called = {
            n.func.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "release_rpm" in called, f"L{node.lineno}: RPM 을 되돌리지 않는다"
        hits += 1
    assert hits == 1, f"tpm_result 거부 분기를 {hits}개 찾았다"


def test_codex_path_is_eligible_for_downgrade():
    """⚠️ Codex 는 ``/v1/responses`` 로만 온다.

    빠져 있으면 그 클라이언트만 예산 임계 강등을 통째로 무시한다 — 그리고 관리자는 규칙이
    활성이라고 본다.
    """
    from app.middleware.downgrade import DOWNGRADE_PATH_PREFIXES, _path_eligible

    assert "/v1/responses" in DOWNGRADE_PATH_PREFIXES
    assert _path_eligible("/v1/responses")
    assert _path_eligible("/v1/messages")
    # 대조군 — 관계없는 경로는 여전히 제외돼야 한다.
    assert not _path_eligible("/health/ready")
    assert not _path_eligible("/admin/models")


def test_the_responses_path_is_treated_as_an_openai_body_shape():
    """본문의 모델 필드 이름 판정에도 들어가야 한다 — 아니면 rewrite 대상을 못 찾는다."""
    from app.middleware.downgrade import _is_openai_path

    assert _is_openai_path("/v1/responses"), (
        "responses 를 OpenAI 본문 형태로 보지 않으면 model 키를 찾지 못해 강등이 무동작이다"
    )
