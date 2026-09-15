# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""실패로 끝난 요청이 ``usage_logs`` 에 남는지 — 그리고 그 기록이 예산을 건드리지 않는지.

무엇이 문제였나
---------------
라우터의 실패 반환은 ``finalize`` 를 부르지 않았다. 그래서 실패한 요청은 ``usage_logs``
에 행이 **아예 없었다**. 워커 쪽에는 짝이 되는 결함이 있었다 — 남은 성공 행의 status 를
``"SUCCESS"`` 로 하드코딩해 넣었다.

둘이 겹치면 admin-api 의 세 모니터링 엔드포인트가 계산하는 ``error_rate_pct`` 는 분자도
0, 분모도 성공뿐이라 **구조적으로 항상 0.00%** 다. 결측보다 나쁘다: admin-ui 는 그 값을
색으로 칠하므로, 상류가 절반씩 5xx 를 뱉는 중에도 운영자의 건강 화면이 정상이라고
적극적으로 주장한다.

이 파일은 게이트웨이 쪽(기록을 발행하는가)을, 워커 쪽 회귀 테스트
``cost-recorder-worker/tests/regression/test_medium_usage_status_round_trip.py`` 가 DB
왕복과 집계식을 담당한다.

⚠️ 가장 위험한 회귀는 "기록이 없다" 가 아니라 **"기록하면서 예약까지 또 되돌린다"** 다.
   예약은 ``release_reservations`` 가 이미 비-2xx 에서 되돌린다. 여기서 한 번 더 되돌리면
   사용자에게 남의 헤드룸을 주게 된다. 그래서 이 파일은 발행 여부만이 아니라 **무엇을
   하지 않는지**도 고정한다.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.cost_recorder import CostRecorder, usage_status_for

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


# ─────────────────────────────────────────────────────────────────────────────
# 1. HTTP 상태 → enum 라벨
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("http_status", "label"),
    [
        (200, "SUCCESS"),
        (201, "SUCCESS"),
        (299, "SUCCESS"),
        (400, "ERROR"),
        (403, "ERROR"),
        (429, "ERROR"),
        (500, "ERROR"),
        (502, "ERROR"),
        (503, "ERROR"),
        (408, "TIMEOUT"),
        (504, "TIMEOUT"),
        (524, "TIMEOUT"),
    ],
)
def test_the_status_mapping_only_produces_labels_the_enum_has(http_status: int, label: str):
    """``usage.usage_status`` 는 세 라벨뿐이다 — 그 밖의 값은 워커의 CAST 에서 거부된다."""
    assert usage_status_for(http_status) == label


def test_timeout_is_distinguished_from_a_plain_error():
    """⚠️ enum 이 TIMEOUT 을 따로 둔 이유가 있다 — 재시도 대상인지가 다르다.

    전부 ERROR 로 뭉개면 라벨이 존재할 이유가 없어지고, 운영자는 상류 지연과 상류 거부를
    같은 신호로 보게 된다.
    """
    assert usage_status_for(504) != usage_status_for(502)


# ─────────────────────────────────────────────────────────────────────────────
# 2. record_failure 가 실제로 무엇을 발행하는가
# ─────────────────────────────────────────────────────────────────────────────


class _CapturingRedis:
    """XADD 만 받는다. eval/evalsha/zrem 이 불리면 그 자리에서 실패시킨다."""

    def __init__(self):
        self.added: list[dict] = []
        self.forbidden: list[str] = []

    async def xadd(self, key, fields, **kw):
        self.added.append({"key": key, "fields": fields, "kw": kw})
        return "1-1"

    def __getattr__(self, name):
        # 예산 차감/예약 해제에 쓰이는 호출은 전부 여기로 떨어진다.
        async def _forbidden(*a, **k):
            self.forbidden.append(name)
            return None

        return _forbidden


class _Auth:
    user_id = "11111111-1111-1111-1111-111111111111"
    team_id = "22222222-2222-2222-2222-222222222222"
    dept_id = "33333333-3333-3333-3333-333333333333"
    sso_subject = "sub-1"


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


async def _publish(http_status: int, **kw) -> dict:
    redis = _CapturingRedis()
    await CostRecorder().record_failure(
        redis,
        _Auth(),
        _model_config(),
        request_id="req-fail-1",
        http_status=http_status,
        duration_ms=1234,
        **kw,
    )
    assert len(redis.added) == 1, f"XADD 가 {len(redis.added)}회 — 정확히 1회여야 한다"
    assert redis.forbidden == [], (
        f"기록 경로가 {redis.forbidden} 를 호출했다 — 예산 차감/예약 해제는 여기서 하지 "
        "않는다(release_reservations 가 이미 되돌린다, 이중 해제가 된다)"
    )
    return json.loads(redis.added[0]["fields"]["payload"])


async def test_a_failed_request_is_published_with_an_error_status():
    entry = await _publish(502)
    assert entry["status"] == "ERROR", entry["status"]
    assert entry["request_id"] == "req-fail-1"
    assert entry["latency_ms"] == 1234, "실제 소요 시간이 들어가야 한다"


async def test_a_timed_out_request_is_published_as_timeout():
    entry = await _publish(504)
    assert entry["status"] == "TIMEOUT", entry["status"]


async def test_the_failure_row_carries_no_tokens_and_no_cost():
    """⚠️ 상류가 거부한 요청에는 청구할 것이 없다.

    비용이 0 이 아니면 워커의 가산 UPSERT 가 사용자의 월 사용액을 늘리고, 그 값은
    게이트웨이가 Redis degrade 시 읽는 진실의 원천이다 — 실패만 반복한 사용자가 예산을
    소진하게 된다.
    """
    entry = await _publish(500)
    assert Decimal(entry["cost_usd"]) == Decimal("0"), entry["cost_usd"]
    assert entry["input_tokens"] == 0
    assert entry["output_tokens"] == 0
    assert entry["cache_creation_tokens"] == 0
    assert entry["cache_read_tokens"] == 0


async def test_the_attribution_fields_survive_so_the_failure_can_be_traced():
    """어느 클라이언트·어느 모델이 실패했는지가 없으면 오류율은 숫자일 뿐 조사가 안 된다."""
    entry = await _publish(
        503,
        client="codex",
        downgraded_from="claude-opus-5",
        availability_fallback_from="claude-sonnet-5",
        is_stream=True,
    )
    assert entry["client"] == "codex"
    assert entry["model_alias"] == "claude-opus"
    assert entry["downgraded_from"] == "claude-opus-5"
    assert entry["availability_fallback_from"] == "claude-sonnet-5"
    assert entry["is_streaming"] is True
    assert entry["sso_subject"] == "sub-1"


async def test_the_period_and_date_buckets_are_present():
    """워커가 이 두 값을 그대로 키로 쓴다 — 비면 집계가 어긋난다."""
    entry = await _publish(500)
    assert len(entry["period"]) == 7 and entry["period"][4] == "-", entry["period"]
    assert len(entry["date"]) == 10, entry["date"]


async def test_a_dead_redis_does_not_turn_a_502_into_a_500():
    """기록 실패가 이미 만들어진 오류 응답을 바꿀 이유는 없다."""

    class _Dead:
        async def xadd(self, *a, **k):
            raise RuntimeError("redis down")

    await CostRecorder().record_failure(
        _Dead(),
        _Auth(),
        _model_config(),
        request_id="req-dead",
        http_status=502,
        duration_ms=5,
    )


async def test_a_failed_xadd_is_spooled_so_the_record_is_not_lost():
    """성공 기록과 같은 발행 경로를 쓴다 — dead-letter spool 도 그대로 붙는다."""

    class _Dead:
        async def xadd(self, *a, **k):
            raise RuntimeError("redis down")

    class _Spool:
        def __init__(self):
            self.items: list[str] = []

        def enqueue(self, payload):
            self.items.append(payload)

    spool = _Spool()
    await CostRecorder(spool=spool).record_failure(
        _Dead(),
        _Auth(),
        _model_config(),
        request_id="req-spool",
        http_status=500,
        duration_ms=5,
    )
    assert len(spool.items) == 1, "실패 기록이 spool 을 타지 않았다 — Redis 장애 중 유실된다"
    assert json.loads(spool.items[0])["status"] == "ERROR"


async def test_an_empty_request_id_is_not_published():
    """request_id 는 usage_logs 의 UNIQUE 키다 — 빈 값은 dedup 이 성립하지 않는다."""
    redis = _CapturingRedis()
    await CostRecorder().record_failure(
        redis,
        _Auth(),
        _model_config(),
        request_id="",
        http_status=500,
        duration_ms=5,
    )
    assert redis.added == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. 배선 — 실패 출구가 빠지지 않았는지
# ─────────────────────────────────────────────────────────────────────────────


def _tree(rel: str) -> ast.Module:
    src = (_SRC / rel).read_text(encoding="utf-8")
    assert len(src) > 1500, f"{rel} 가 너무 짧다 — 경로 확인"
    return ast.parse(src)


def _fn(tree: ast.Module, name: str):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"{name} 를 찾지 못했다")


def _calls(node, attr: str) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    ]


def test_the_anthropic_route_records_before_any_of_its_failure_exits():
    """⚠️ 출구마다 넣는 대신 **한 곳**에서 덮는다 — 그리고 그 곳이 앞이어야 한다.

    messages 라우트의 실패 출구는 네 갈래다: all_open 503, bytes 페이로드(폴백 전멸),
    스트리밍 오류 제너레이터, usage 가 비어 finalize 가 스킵되는 비스트리밍 오류. 출구마다
    흩어 놓으면 다음에 출구가 하나 늘 때 조용히 빠진다 — 실제로 그렇게 빠져 있었다.

    그래서 기록 호출이 **첫 실패 출구보다 앞선 줄**에 있는지를 본다. 호출 존재만 보면
    출구 하나 뒤에 두어도 통과한다.

    ⚠️ 경계는 폴백 루프의 결과를 받는 줄(``status = result.status``)이다. 그보다 앞의
       400 들(잘못된 JSON, model 누락, 알 수 없는 alias)은 **의도적으로 제외한다** —
       모델을 호출하지도 않은 요청이고, 그것까지 세면 모니터링 화면의 모델별 오류율에
       "사용자가 JSON 을 잘못 보냄" 이 섞인다. 오류율은 상류 건강 지표다.
    """
    tree = _tree("routers/messages.py")
    fn = _fn(tree, "messages")
    recorded = _calls(fn, "record_failure")
    assert recorded, "messages 라우트가 실패를 기록하지 않는다"

    boundary = None
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "status" for t in n.targets)
            and isinstance(n.value, ast.Attribute)
            and n.value.attr == "status"
        ):
            boundary = n.lineno
    assert boundary, "`status = result.status` 를 찾지 못했다 — 이 검사의 전제가 깨졌다"
    assert min(recorded) > boundary, (
        f"기록(L{min(recorded)})이 폴백 결과(L{boundary})보다 앞이다 — status 를 모른다"
    )

    # 상류가 관여한 실패 출구 = 경계 이후의 JSONResponse 반환
    returns = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", None) == "JSONResponse"
        and n.lineno > boundary
    ]
    assert returns, "경계 이후의 실패 반환을 찾지 못했다 — 이 검사의 전제가 깨졌다"
    assert min(recorded) < min(returns), (
        f"기록(L{min(recorded)})이 첫 실패 출구(L{min(returns)}) 뒤에 있다 — "
        "그 출구로 나가는 요청은 기록되지 않는다"
    )


def test_the_anthropic_route_records_on_any_non_2xx_not_a_hand_picked_list():
    """⚠️ 상태 코드 목록으로 판정하면 목록에 없는 실패가 조용히 빠진다.

    ``_FALLBACK_STATUSES`` 로 판정했다가 400 을 놓친 전례가 이 저장소에 있다.
    """
    src = (_SRC / "routers" / "messages.py").read_text(encoding="utf-8")
    idx = src.find("record_failure")
    assert idx > 0
    window = src[max(0, idx - 600) : idx]
    assert "200 <= status < 300" in window, (
        "기록 조건이 2xx 여부가 아니다 — 특정 상태 목록으로 판정하면 목록 밖 실패를 놓친다"
    )


@pytest.mark.parametrize("handler", ["_handle_openai", "_handle_responses"])
def test_both_openai_handlers_record_where_finalize_is_skipped(handler: str):
    """⚠️ 한쪽만 하면 그 방언의 클라이언트만 오류율에서 사라진다.

    Codex 는 ``/v1/responses``(=``_handle_responses``)로만 온다 — 비용이 가장 큰
    클라이언트가 빠지면 오류율은 사실상 다른 방언 전용 지표가 된다.
    """
    tree = _tree("routers/openai_compat.py")
    fn = _fn(tree, handler)
    assert _calls(fn, "record_failure"), f"{handler} 가 실패를 기록하지 않는다"


@pytest.mark.parametrize("handler", ["_handle_openai", "_handle_responses"])
def test_the_record_sits_in_the_same_branch_that_releases_reservations(handler: str):
    """기록과 예약 해제는 **같은** 조건(=finalize 를 타지 않는 경로)에 걸려야 한다.

    조건이 갈라지면 한쪽만 도는 상태가 생기고, 그 상태는 재현 조건이 좁아 오래 남는다.
    """
    tree = _tree("routers/openai_compat.py")
    fn = _fn(tree, handler)
    for node in ast.walk(fn):
        if not isinstance(node, ast.If) or not node.orelse:
            continue
        orelse_calls = {
            n.func.attr
            for b in node.orelse
            for n in ast.walk(b)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        orelse_names = {
            n.func.id
            for b in node.orelse
            for n in ast.walk(b)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if "release_reservations" in orelse_names:
            assert "record_failure" in orelse_calls, (
                f"{handler} L{node.lineno}: 예약은 되돌리는데 기록은 하지 않는다"
            )
            return
    raise AssertionError(f"{handler}: release_reservations 를 하는 else 분기를 찾지 못했다")


def test_the_recorder_does_not_deduct_budget_on_the_failure_path():
    """⚠️ 이 파일에서 가장 중요한 고정이다.

    ``record_failure`` 가 ``finalize`` 를 재사용하도록 "간단히" 바꾸면 예산 차감과
    settle 이 함께 돌고, ``release_reservations`` 와 겹쳐 **이중 해제**가 된다. 그러면
    사용자는 자기 것이 아닌 헤드룸을 얻는다.
    """
    tree = _tree("services/cost_recorder.py")
    fn = _fn(tree, "record_failure")
    called_attrs = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    for forbidden in ("finalize", "release_reservations", "settle_cost", "settle_tpm"):
        assert forbidden not in called_attrs, (
            f"record_failure 가 {forbidden} 을 부른다 — 기록 경로는 기록만 해야 한다"
        )
    assert "_publish_to_stream" in called_attrs, "발행 경로를 쓰지 않는다"
