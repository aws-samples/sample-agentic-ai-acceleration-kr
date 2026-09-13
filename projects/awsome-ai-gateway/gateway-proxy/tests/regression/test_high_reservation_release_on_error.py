# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""실패한 요청이 **예약을 되돌리는지**.

예약 모델
---------
``enforce_rate_limits`` 는 요청을 받아들일 때 TPM 과 비용(CPM/CPH)을 **미리** 깎는다.
비용 예약은 ``max_tokens`` 기준의 과대 추정이고, 응답 뒤 실제 사용량으로 정산(환불)된다.
그래서 정산되지 않은 요청은 사용자의 분/시간 한도를 **실제로 쓴 적 없는 양만큼** 계속
물고 있는다.

무엇이 문제였나 (네 갈래로 보고된 같은 결함)
--------------------------------------------
  1. ``release_reservations`` 가 502/503/504 에서만 불렸다 → 상류 400/404/422/429/500
     에서는 되돌리는 곳이 없었다(라우터의 ``finalize`` 도 usage 0 이라 건너뛴다).
  2. ``finalize`` 의 zero-usage 경로는 **TPM 만** 해제하고 비용은 남겼다.
  3. ``/v1/chat/completions`` 와 ``/v1/responses`` 는 폴백 루프가 없어 unwind 가 아예
     돌지 않았다.
  4. ``/model/*/invoke`` 도 같은 이유로 남았다.

증상: 잘못된 tool 스키마나 컨텍스트 초과로 400 을 연속 받은 사용자가 **실제 지출 $0** 로
자기 CPM/CPH 를 소진하고, 그 분/시간이 끝날 때까지 스스로 429 를 맞는다. 청구서에는
아무것도 없으므로 "왜 429 냐" 에 답할 근거가 화면에 없다.

⚠️ 이 파일의 핵심 검사는 **실 Redis** 를 요구한다(``REDIS_PROOF_URL``). 예약/환불은
   ``incrbyfloat`` 로 카운터를 움직이는 것이고, 가짜 Redis 는 그 값을 검증해 주지 않는다 —
   호출됐는지만 보면 "부호가 반대인 환불" 도 통과한다.

실행:
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 \\
      pytest tests/regression/test_high_reservation_release_on_error.py
"""

from __future__ import annotations

import ast
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.fallback_loop import release_reservations

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")


# ─────────────────────────────────────────────────────────────────────────────
# 1. 실 Redis — 카운터가 정말로 되돌아가는가
# ─────────────────────────────────────────────────────────────────────────────

_redis_required = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason=(
        "REDIS_PROOF_URL 미설정 — 실 Redis 필요. 환불은 incrbyfloat 로 카운터를 "
        "움직이는 것이라, 호출 여부만 보는 가짜로는 부호가 반대인 환불도 통과한다."
    ),
)


class _Auth:
    def __init__(self, user_id: str, team_id: str):
        self.user_id = user_id
        self.team_id = team_id


def _cost_keys(user_id: str, team_id: str) -> list[str]:
    now = int(time.time())
    return [
        f"rl:cost:user:{{{user_id}}}:cpm:{(now // 60) * 60}",
        f"rl:cost:user:{{{user_id}}}:cph:{(now // 3600) * 3600}",
        f"rl:cost:team:{{{team_id}}}:cpm:{(now // 60) * 60}",
        f"rl:cost:team:{{{team_id}}}:cph:{(now // 3600) * 3600}",
    ]


async def _read_floats(redis, keys: list[str]) -> list[float]:
    out = []
    for k in keys:
        raw = await redis.get(k)
        out.append(float(raw) if raw is not None else 0.0)
    return out


@_redis_required
async def test_release_refunds_the_cost_reservation_on_real_redis():
    """⚠️ 이 파일의 핵심. 예약된 비용이 **정확히** 되돌아가야 한다.

    부분 환불이나 부호 반대(추가 차감)는 카운터 값을 봐야만 드러난다.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    user_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    keys = _cost_keys(user_id, team_id)
    reserved = Decimal("2.50")
    try:
        # 예약을 흉내낸다 — enforce_rate_limits 가 하는 것과 같은 방향(가산).
        for k in keys:
            await r.incrbyfloat(k, float(reserved))
        before = await _read_floats(r, keys)
        assert all(abs(v - 2.50) < 1e-9 for v in before), f"예약 셋업 실패: {before}"

        state = {
            "rate_limit_state": {
                "tpm_descriptors": [],
                "tpm_reserved": 0,
                "cost_reserved": reserved,
            }
        }
        await release_reservations(redis=r, state=state, auth_context=_Auth(user_id, team_id))

        after = await _read_floats(r, keys)
        assert all(abs(v) < 1e-9 for v in after), (
            f"환불 후 카운터가 0 이 아니다: {after} — 예약이 남아 사용자 한도를 물고 있다"
        )
        # 상태가 지워져야 finalize 가 이중 정산하지 않는다(멱등성의 근거).
        assert "rate_limit_state" not in state
    finally:
        for k in keys:
            await r.delete(k)
        await r.aclose()


@_redis_required
async def test_release_is_idempotent_and_does_not_over_refund():
    """두 번 불러도 한 번만 환불돼야 한다.

    이 성질이 없으면 "혹시 몰라 한 번 더" 부르는 호출자가 사용자 한도를 **음수**로
    만들어 다른 사람 몫을 빌려주게 된다.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    user_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    keys = _cost_keys(user_id, team_id)
    try:
        for k in keys:
            await r.incrbyfloat(k, 1.0)
        state = {
            "rate_limit_state": {
                "tpm_descriptors": [],
                "tpm_reserved": 0,
                "cost_reserved": Decimal("1.0"),
            }
        }
        auth = _Auth(user_id, team_id)
        await release_reservations(redis=r, state=state, auth_context=auth)
        await release_reservations(redis=r, state=state, auth_context=auth)
        after = await _read_floats(r, keys)
        assert all(abs(v) < 1e-9 for v in after), f"이중 환불로 음수가 됐다: {after}"
    finally:
        for k in keys:
            await r.delete(k)
        await r.aclose()


@_redis_required
async def test_finalize_zero_usage_releases_cost_not_just_tpm():
    """⚠️ 결함 #2. 응답을 한 토큰도 못 받은 요청도 비용 예약을 돌려받아야 한다.

    옛 구현은 TPM 만 환불했다. 그러면 두 한도 중 하나만 정상으로 보여 원인 추적이 더
    어렵다 — "TPM 은 여유 있는데 왜 429 냐" 가 된다.
    """
    import redis.asyncio as aioredis

    from app.schemas.domain import TokenUsage
    from app.services.cost_recorder import CostRecorder

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    user_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    keys = _cost_keys(user_id, team_id)
    try:
        for k in keys:
            await r.incrbyfloat(k, 0.75)

        recorder = CostRecorder()
        await recorder.finalize(
            r,
            _Auth(user_id, team_id),
            _model_config(),
            TokenUsage(),  # 전부 0 — disconnect 경로
            "req-zero",
            True,
            123,
            rate_limit_state={
                "tpm_descriptors": [],
                "tpm_reserved": 0,
                "cost_reserved": Decimal("0.75"),
            },
        )
        after = await _read_floats(r, keys)
        assert all(abs(v) < 1e-9 for v in after), (
            f"zero-usage 경로가 비용 예약을 돌려주지 않았다: {after}"
        )
    finally:
        for k in keys:
            await r.delete(k)
        await r.aclose()


def _model_config():
    from datetime import datetime

    from app.schemas.domain import (
        ApiFormat,
        ModelConfigSchema,
        ModelPricingSchema,
        ModelStatus,
        ProviderType,
    )

    return ModelConfigSchema(
        provider_model_id="anthropic.claude-opus",
        alias="claude-opus",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.ANTHROPIC_MESSAGES,
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")
        ),
        status=ModelStatus.ACTIVE,
        created_at=datetime(2026, 1, 1),
    )


async def test_release_is_a_noop_without_a_reservation():
    """예약이 없으면 아무것도 하지 않는다 — Redis 가 없어도 안전하다."""
    state: dict = {}
    await release_reservations(redis=None, state=state, auth_context=_Auth("u", "t"))
    await release_reservations(redis=object(), state=state, auth_context=_Auth("u", "t"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. 배선 — 되돌릴 곳을 빠뜨리지 않았는지
# ─────────────────────────────────────────────────────────────────────────────


def _tree(rel: str) -> ast.Module:
    src = (_SRC / rel).read_text(encoding="utf-8")
    assert len(src) > 1500, f"{rel} 가 너무 짧다 — 경로 확인"
    return ast.parse(src)


def _calls(node, name: str) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_fallback_loop_releases_on_every_non_2xx_not_just_502_503_504():
    """⚠️ 결함 #1. 조건이 폴백 상태 집합으로 좁혀져 있으면 400/429/500 이 새어 나간다.

    ``_FALLBACK_STATUSES`` 안에서만 되돌리던 것이 원래 코드였다. 상태 집합이 아니라
    "2xx 가 아니면" 으로 판정하는지 본다.
    """
    tree = _tree("services/fallback_loop.py")
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_fallback_loop"
    )
    releases = _calls(fn, "release_reservations")
    assert releases, "run_fallback_loop 이 예약을 되돌리지 않는다"

    # 되돌리기를 감싸는 If 의 조건이 2xx 범위 비교여야 한다.
    guarded_by_range = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if not _calls(node, "release_reservations"):
            continue
        test_src = ast.dump(node.test)
        # `not (200 <= status < 300)` 형태
        if "200" in test_src and "300" in test_src:
            guarded_by_range = True
    assert guarded_by_range, (
        "예약 되돌리기가 2xx 범위 판정으로 감싸여 있지 않다 — 상태 집합(502/503/504)으로 "
        "좁히면 상류 400/404/422/429/500 에서 예약이 남는다"
    )


@pytest.mark.parametrize(
    ("router", "func"),
    [
        ("routers/openai_compat.py", "_handle_openai"),
        ("routers/openai_compat.py", "_handle_responses"),
    ],
)
def test_openai_handlers_release_on_both_branches(router: str, func: str):
    """⚠️ 결함 #3. 이 라우트들에는 폴백 루프가 없다 — 스스로 되돌려야 한다.

    스트리밍(비-2xx 로 시작한 스트림)과 비스트리밍(usage 없는 응답) **양쪽**이 필요하다.
    한쪽만 하면 그 방언의 그 모드에서만 새고, 재현 조건이 좁아 오래 남는다.
    """
    tree = _tree(router)
    fn = next(
        n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == func
    )
    releases = _calls(fn, "release_reservations")
    assert len(releases) >= 2, (
        f"{func}: 예약 되돌리기가 {len(releases)}곳뿐이다 — 스트리밍/비스트리밍 둘 다 필요"
    )


def test_bedrock_route_releases_too():
    """결함 #4. `/model/*` 패스스루도 같은 예약을 쓴다."""
    tree = _tree("routers/bedrock.py")
    fns = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
    total = sum(len(_calls(fn, "release_reservations")) for fn in fns)
    assert total >= 2, f"bedrock 라우트의 되돌리기가 {total}곳뿐이다"


def test_cost_recorder_zero_usage_path_settles_cost():
    """결함 #2 의 구조 검사 — zero-usage 분기 안에 ``settle_cost`` 가 있어야 한다.

    실 Redis 검사가 skip 되는 환경(로컬)에서도 이 배선은 지켜지게 한다.
    """
    tree = _tree("services/cost_recorder.py")
    zero_branch = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        attrs = {n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)}
        if {"total_tokens", "input_tokens", "output_tokens"} <= attrs:
            zero_branch = node
            break
    assert zero_branch is not None, "zero-usage 분기를 찾지 못했다 — 전제가 깨졌다"
    called = {
        n.func.attr
        for n in ast.walk(zero_branch)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "settle_tpm" in called, "zero-usage 분기가 TPM 을 돌려주지 않는다"
    assert "settle_cost" in called, (
        "zero-usage 분기가 **비용**을 돌려주지 않는다 — TPM 만 환불하면 두 한도 중 하나만 "
        "정상으로 보여 원인 추적이 더 어려워진다"
    )


def test_tpm_and_cost_releases_are_in_separate_try_blocks():
    """한 try 에 묶으면 TPM 쪽 예외가 비용 해제를 건너뛰게 만든다.

    그러면 정확히 원래의 결함(TPM 만 환불)으로 조용히 되돌아간다.
    """
    tree = _tree("services/cost_recorder.py")
    zero_branch = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            attrs = {n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)}
            if {"total_tokens", "input_tokens", "output_tokens"} <= attrs:
                zero_branch = node
                break
    assert zero_branch is not None
    tries = [n for n in ast.walk(zero_branch) if isinstance(n, ast.Try)]
    with_tpm = [t for t in tries if "settle_tpm" in ast.dump(t)]
    with_cost = [t for t in tries if "settle_cost" in ast.dump(t)]
    assert with_tpm and with_cost, "두 해제가 각각 try 로 감싸여 있지 않다"
    assert not (set(map(id, with_tpm)) & set(map(id, with_cost))), (
        "TPM 과 비용 해제가 같은 try 안에 있다 — TPM 이 터지면 비용이 환불되지 않는다"
    )
