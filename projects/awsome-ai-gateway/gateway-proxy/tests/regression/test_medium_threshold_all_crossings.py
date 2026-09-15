# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""예산 임계값 알림 — 넘은 것을 **전부**, 넘은 **스코프별로**.

결함 1 — 한 요청이 여러 임계값을 넘으면 가장 낮은 것만 알렸다
------------------------------------------------------------
``budget_deduct.lua`` 의 교차 판정이 첫 번째로 맞는 임계값에서 ``break`` 했고, 임계값
목록은 오름차순({80, 90, 100})이었다. 그래서 한 요청이 70% → 105% 로 뛰면 세 임계값을
모두 넘었는데 **80% 알림만** 나갔다 — 예산이 소진됐다는 사실(100%)은 아무에게도 전달되지
않는다.

큰 요청 한 건이면 재현되고(캐시 미스 + 큰 max_tokens), 알림의 부재는 "예산을 안 썼다" 와
구별되지 않으므로 알아챌 방법이 없다. 그리고 나쁜 방향으로만 틀린다: 받은 알림이 "80%" 라
운영자는 아직 여유가 있다고 판단한다.

결함 2 — 팀/앱 스코프의 교차는 계산해서 버렸다
----------------------------------------------
``cost_recorder`` 는 스코프별로 같은 Lua 를 호출한다. USER 호출의 반환값만 읽고 팀과
per-app 호출은 ``await redis.eval(...)`` 로 **반환값을 버렸다.** 담을 곳도 없었다 —
스트림 엔트리에는 단일 ``threshold_triggered`` 필드뿐이었고, 워커의 알림 payload 는
``target_type`` 을 ``"user"`` 로 하드코딩했다.

즉 팀 예산 알림과 앱별 예산 알림은 **구조적으로 발송 불가능**했다. 팀 한도를 다 쓴 팀에게
아무 신호도 가지 않는다.

부수 결함 — 메일이 잘못된 금액을 말했다
---------------------------------------
알림 payload 의 ``current_used_usd`` 에 **그 요청 하나의 비용**이 실렸다
(``str(e.cost_usd)``). 누적 사용액이 아니라 $0.03 같은 값이다. Lua 는 누적값을 이미
알고 있으므로 이제 그것을 함께 실어 보낸다.

⚠️ 교차 판정은 **실 Redis 에서 실제 Lua 를 돌려** 확인한다. ``break`` 하나로 되돌아가는
   결함이고, 파이썬 쪽에서 흉내낸 판정으로는 스크립트가 무엇을 하는지 알 수 없다. cjson 의
   "빈 테이블은 JSON 객체" 성질도 실제 인코딩에서만 드러난다.
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.cost_recorder import _threshold_events

REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

_redis_required = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason=(
        "REDIS_PROOF_URL 미설정 — 실 Redis 필요. 교차 판정은 Lua 안에 있고, cjson 의 "
        "빈-테이블 인코딩은 실제 인코딩에서만 드러난다."
    ),
)

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "src" / "app" / "redis_scripts" / "budget_deduct.lua"
)


async def _deduct(redis, *, usage_key, config_key, cost, limit, thresholds):
    """설정 키를 세팅하고 실제 Lua 를 EVAL 한다."""
    await redis.set(config_key, json.dumps({"limit_usd": str(limit), "thresholds": thresholds}))
    raw = await redis.eval(_SCRIPT.read_text(encoding="utf-8"), 2, usage_key, config_key, str(cost))
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Lua — 넘은 것을 전부 모은다
# ─────────────────────────────────────────────────────────────────────────────


@_redis_required
async def test_a_single_request_that_crosses_three_thresholds_reports_all_three():
    """⚠️ 이 파일의 핵심. 70% → 105% 는 80/90/100 을 모두 넘는다.

    예전에는 ``break`` 때문에 80 만 보고했고, **소진(100%) 이 전달되지 않았다.**
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = uuid.uuid4().hex
    usage_key, config_key = f"budget:user:{{{uid}}}:2026-06", f"budget:config:user:{{{uid}}}"
    try:
        await r.set(usage_key, "70")  # 한도 100 의 70%
        res = await _deduct(
            r, usage_key=usage_key, config_key=config_key,
            cost=35, limit=100, thresholds=[80, 90, 100],
        )
        assert res["crossed"] == [80, 90, 100], (
            f"넘은 임계값이 {res.get('crossed')} — 세 개 모두여야 한다. 하나만 보고하면 "
            "운영자는 소진 사실을 모른 채 '80% 도달' 만 받는다"
        )
        assert res["threshold_triggered"] == 100, (
            f"구버전 호환 필드가 {res['threshold_triggered']} — 넘은 것 중 **가장 높은** "
            "값이어야 한다(낮은 값을 주면 원래 결함으로 되돌아간다)"
        )
        assert float(res["new_used"]) == pytest.approx(105.0)
        assert float(res["limit"]) == pytest.approx(100.0)
    finally:
        await r.delete(usage_key, config_key)
        await r.aclose()


@_redis_required
async def test_crossing_exactly_one_threshold_reports_only_that_one():
    """⚠️ 대조군. 위 단정이 "항상 전부" 로 통과하는 것이 아님을 보인다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = uuid.uuid4().hex
    usage_key, config_key = f"budget:user:{{{uid}}}:2026-06", f"budget:config:user:{{{uid}}}"
    try:
        await r.set(usage_key, "70")
        res = await _deduct(
            r, usage_key=usage_key, config_key=config_key,
            cost=12, limit=100, thresholds=[80, 90, 100],
        )
        assert res["crossed"] == [80], res.get("crossed")
        assert res["threshold_triggered"] == 80
    finally:
        await r.delete(usage_key, config_key)
        await r.aclose()


@_redis_required
async def test_no_crossing_omits_the_key_rather_than_sending_an_empty_array():
    """⚠️ cjson 은 **빈 Lua 테이블을 JSON 객체 ``{}``** 로 인코딩한다(배열이 아니다).

    빈 목록을 그대로 실으면 파이썬 쪽 ``isinstance(x, list)`` 가 False 가 된다 — 같은
    함정으로 ``app_clients`` 게이트가 조용히 닫힌 전례가 이 저장소에 있다. 그래서 비어
    있으면 키를 아예 넣지 않고, 읽는 쪽이 부재를 "교차 없음" 으로 다룬다.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = uuid.uuid4().hex
    usage_key, config_key = f"budget:user:{{{uid}}}:2026-06", f"budget:config:user:{{{uid}}}"
    try:
        await r.set(usage_key, "10")
        res = await _deduct(
            r, usage_key=usage_key, config_key=config_key,
            cost=5, limit=100, thresholds=[80, 90, 100],
        )
        assert "crossed" not in res, (
            f"빈 교차를 실었다: {res['crossed']!r} — cjson 이 이것을 객체로 인코딩하면 "
            "읽는 쪽의 리스트 판정이 조용히 실패한다"
        )
        assert res["threshold_triggered"] is None
    finally:
        await r.delete(usage_key, config_key)
        await r.aclose()


@_redis_required
async def test_an_unsorted_threshold_list_still_reports_in_ascending_order():
    """운영자가 임의 순서로 저장할 수 있다 — Lua 가 입력 정렬에 의존하면 조용히 어긋난다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = uuid.uuid4().hex
    usage_key, config_key = f"budget:user:{{{uid}}}:2026-06", f"budget:config:user:{{{uid}}}"
    try:
        await r.set(usage_key, "40")
        res = await _deduct(
            r, usage_key=usage_key, config_key=config_key,
            cost=60, limit=100, thresholds=[100, 50, 90],
        )
        assert res["crossed"] == [50, 90, 100], res.get("crossed")
        assert res["threshold_triggered"] == 100
    finally:
        await r.delete(usage_key, config_key)
        await r.aclose()


@_redis_required
async def test_no_limit_configured_never_crosses():
    """한도 0/미설정에서 교차를 만들면 예산 없는 사용자에게 알림이 쏟아진다."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    uid = uuid.uuid4().hex
    usage_key, config_key = f"budget:user:{{{uid}}}:2026-06", f"budget:config:user:{{{uid}}}"
    try:
        res = await _deduct(
            r, usage_key=usage_key, config_key=config_key,
            cost=10, limit=0, thresholds=[80],
        )
        assert "crossed" not in res
        assert res["threshold_triggered"] is None
    finally:
        await r.delete(usage_key, config_key)
        await r.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# 2. 이벤트 변환 — 누적 금액과 스코프
# ─────────────────────────────────────────────────────────────────────────────


def test_events_carry_the_accumulated_spend_not_the_request_cost():
    """⚠️ 알림 메일이 "현재 $0.03 사용" 이라고 말하던 결함이 여기서 막힌다."""
    events = _threshold_events(
        {"crossed": [80, 100], "new_used": 105.0, "limit": 100.0}, "user"
    )
    assert [e.threshold_pct for e in events] == [80, 100]
    assert all(e.used_usd == Decimal("105.0") for e in events), [str(e.used_usd) for e in events]
    assert all(e.limit_usd == Decimal("100.0") for e in events)
    assert all(e.scope == "user" for e in events)
    assert all(e.client is None for e in events)


def test_the_client_scope_records_which_app_crossed():
    """어느 앱의 예산인지 없으면 알림이 어떤 예산 얘기인지 알 수 없다."""
    events = _threshold_events(
        {"crossed": [90], "new_used": 45.0, "limit": 50.0}, "client", "codex"
    )
    assert len(events) == 1
    assert events[0].scope == "client"
    assert events[0].client == "codex"


def test_a_legacy_lua_result_without_crossed_still_produces_one_event():
    """⚠️ 배포 중 파드 혼재 — 한쪽만 새 스크립트를 로드한 구간이 실제로 생긴다.

    그때 구형 결과(``threshold_triggered`` 만)를 버리면 그 구간의 알림이 전부 사라진다.
    """
    events = _threshold_events({"threshold_triggered": 90, "new_used": 91.0}, "team")
    assert len(events) == 1
    assert events[0].threshold_pct == 90
    assert events[0].scope == "team"


def test_no_crossing_produces_no_events():
    """⚠️ 대조군. 이것이 없으면 항상 이벤트를 만들어도 위 테스트들이 통과한다."""
    assert _threshold_events({"new_used": 5.0, "limit": 100.0}, "user") == []
    assert _threshold_events({"threshold_triggered": None}, "user") == []
    assert _threshold_events(None, "user") == []
    # cjson 이 빈 테이블을 객체로 준 경우 — 리스트가 아니므로 무시해야 한다.
    assert _threshold_events({"crossed": {}, "new_used": 5.0}, "user") == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. 배선 — 세 스코프의 반환값을 모두 읽는지
# ─────────────────────────────────────────────────────────────────────────────


def test_every_budget_deduct_call_site_reads_its_result():
    """⚠️ 팀/앱 호출은 ``await redis.eval(...)`` 로 반환값을 **버렸다.**

    AST 로 본다: ``budget_deduct`` 를 EVAL 하는 ``await`` 식이 그 자체로 문(statement)이면
    (= 결과를 어디에도 담지 않으면) 그 스코프의 교차는 영원히 발송되지 않는다.
    """
    import ast

    src = (
        Path(__file__).resolve().parents[2] / "src" / "app" / "services" / "cost_recorder.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    discarded = []
    total = 0
    for node in ast.walk(tree):
        # `await redis.eval(...)` 이 Expr 문으로 홀로 서 있는 경우
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Await)):
            continue
        call = node.value.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        if call.func.attr != "eval":
            continue
        text = ast.unparse(call)
        if "budget_deduct" not in text:
            continue
        total += 1
        discarded.append(node.lineno)
    assert not discarded, (
        f"L{discarded}: budget_deduct 의 결과를 버린다 — 그 스코프의 임계값 교차는 "
        "구조적으로 발송되지 않는다"
    )


def test_all_three_scopes_contribute_events():
    """세 스코프 모두 ``_threshold_events`` 로 들어가야 한다.

    하나라도 빠지면 그 스코프의 알림만 조용히 사라진다.
    """
    import ast

    src = (
        Path(__file__).resolve().parents[2] / "src" / "app" / "services" / "cost_recorder.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    scopes = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_threshold_events"
        ):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                scopes.add(arg.value)
    assert scopes == {"user", "team", "client"}, (
        f"이벤트를 만드는 스코프가 {scopes} — user/team/client 셋 모두여야 한다"
    )


def test_the_stream_entry_can_express_scope_and_multiple_crossings():
    """단일 int 필드로는 이 결함을 고칠 수 없다 — 스키마가 표현할 수 있어야 한다."""
    from app.schemas.cost_stream import CostStreamEntry, ThresholdEvent

    assert "threshold_events" in CostStreamEntry.model_fields
    ev = ThresholdEvent(scope="team", threshold_pct=100, used_usd="81", limit_usd="80")
    assert ev.scope == "team"
    # 구버전 엔트리에는 없으므로 기본값이 빈 목록이어야 한다(필수면 파싱이 깨진다).
    assert CostStreamEntry.model_fields["threshold_events"].is_required() is False
