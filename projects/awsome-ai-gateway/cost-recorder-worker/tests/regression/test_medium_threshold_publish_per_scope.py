# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""임계값 알림이 스코프별·임계값별로 **각각** 발행되는지.

무엇이 문제였나
---------------
발행부는 엔트리마다 ``threshold_triggered`` 단일 값 하나만 보고 한 건을 발행하면서
payload 의 ``target_type`` 을 ``"user"`` 로 하드코딩했다. 그래서 세 가지가 동시에 깨져
있었다:

  1. 한 요청이 80/90/100 을 함께 넘어도 **한 건만** 나갔다(게이트웨이 쪽 Lua 가 첫 교차에서
     멈췄고, 담을 필드도 하나뿐이었다).
  2. 팀·앱 스코프 교차는 발행 대상이 아예 없었다 — 게이트웨이가 그 Lua 결과를 버렸고,
     ``target_type`` 은 무엇이 들어와도 "user" 였다.
  3. ``current_used_usd`` 에 **그 요청 하나의 비용**이 실렸다. 메일이 "현재 $0.03 사용"
     이라고 말한다.

⚠️ ``event_id`` 도 함께 고쳐야 한다. ``request_id`` 만 쓰면 한 요청에서 나온 여러 건이 같은
   id 를 갖고, 수신 측 멱등 처리가 두 번째부터를 중복으로 버린다 — 그러면 정확히 가장
   중요한 100% 알림이 사라진다(80% 가 먼저 나가므로).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from worker.schemas.cost_stream import CostStreamEntry, ThresholdEvent

USER_ID = "11111111-1111-1111-1111-111111111111"
TEAM_ID = "22222222-2222-2222-2222-222222222222"
DEPT_ID = "33333333-3333-3333-3333-333333333333"


class _CapturingRedis:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel, payload):
        self.published.append((channel, json.loads(payload)))

    def pipeline(self, *a, **k):
        class _P:
            def __getattr__(self, _n):
                return lambda *a, **k: None

            async def execute(self):
                return None

        return _P()


def _entry(**kw) -> CostStreamEntry:
    base = dict(
        request_id="req-1",
        user_id=USER_ID,
        team_id=TEAM_ID,
        dept_id=DEPT_ID,
        model_alias="proof-model",
        provider="BEDROCK",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal("0.03"),
        latency_ms=100,
        requested_at="2026-06-01T00:00:00+00:00",
        completed_at="2026-06-01T00:00:01+00:00",
        period="2026-06",
        date="2026-06-01",
    )
    base.update(kw)
    return CostStreamEntry(**base)


def _flusher(redis):
    from worker.batch_flusher import BatchFlusher

    return BatchFlusher(session_factory=None, redis=redis)


def _ev(scope, pct, used="105", limit="100", client=None):
    return ThresholdEvent(
        scope=scope, threshold_pct=pct, used_usd=used, limit_usd=limit, client=client
    )


async def test_three_crossings_in_one_request_produce_three_notifications():
    """⚠️ 예전에는 한 건만 나갔고, 그 한 건은 가장 낮은 임계값이었다."""
    redis = _CapturingRedis()
    entry = _entry(
        threshold_triggered=100,
        threshold_events=[_ev("user", 80), _ev("user", 90), _ev("user", 100)],
    )
    await _flusher(redis)._publish_thresholds([entry])

    assert len(redis.published) == 3, f"발행 {len(redis.published)}건 — 3건이어야 한다"
    pcts = [p["payload"]["threshold_pct"] for _c, p in redis.published]
    assert pcts == [80, 90, 100], pcts


async def test_each_notification_has_a_distinct_event_id():
    """⚠️ 같은 id 면 수신 측 멱등 처리가 100% 알림을 중복으로 버린다."""
    redis = _CapturingRedis()
    entry = _entry(
        threshold_triggered=100,
        threshold_events=[_ev("user", 80), _ev("team", 80), _ev("user", 100)],
    )
    await _flusher(redis)._publish_thresholds([entry])
    ids = [p["event_id"] for _c, p in redis.published]
    assert len(set(ids)) == len(ids), f"event_id 중복: {ids}"
    assert all(ids), "event_id 가 비어 있다"


async def test_the_team_scope_is_reported_as_team_not_user():
    """⚠️ ``target_type`` 하드코딩이 되살아나면 수신자 해석이 사용자 쪽으로 간다."""
    redis = _CapturingRedis()
    await _flusher(redis)._publish_thresholds(
        [_entry(threshold_events=[_ev("team", 100, used="81", limit="80")])]
    )
    assert len(redis.published) == 1
    payload = redis.published[0][1]["payload"]
    assert payload["target_type"] == "team", payload["target_type"]
    assert payload["team_id"] == TEAM_ID


async def test_the_client_scope_names_the_app():
    redis = _CapturingRedis()
    await _flusher(redis)._publish_thresholds(
        [_entry(threshold_events=[_ev("client", 90, used="45", limit="50", client="codex")])]
    )
    payload = redis.published[0][1]["payload"]
    assert payload["target_type"] == "client"
    assert payload["client"] == "codex"


async def test_the_notification_reports_accumulated_spend_not_the_request_cost():
    """⚠️ 이 값이 메일 본문에 쓰이는 종류의 필드다 — 요청 비용이면 사실과 다르다."""
    redis = _CapturingRedis()
    await _flusher(redis)._publish_thresholds(
        [
            _entry(
                cost_usd=Decimal("0.03"),
                threshold_events=[_ev("user", 100, used="105", limit="100")],
            )
        ]
    )
    payload = redis.published[0][1]["payload"]
    assert Decimal(payload["current_used_usd"]) == Decimal("105"), payload["current_used_usd"]
    assert Decimal(payload["limit_usd"]) == Decimal("100")


async def test_an_entry_with_no_crossing_publishes_nothing():
    """⚠️ 대조군. 이것이 없으면 무조건 발행으로 바꿔도 위 테스트들이 통과한다.

    그러면 모든 요청마다 알림이 나가고, 그것은 알림이 오지 않는 것보다 나쁘다(운영자가
    필터를 만들어 전부 버리게 된다).
    """
    redis = _CapturingRedis()
    await _flusher(redis)._publish_thresholds([_entry()])
    assert redis.published == [], redis.published


async def test_a_legacy_entry_without_events_still_publishes_once():
    """배포 중 스트림에 남아 있는 구버전 메시지 — 예전과 동일하게 한 건 발행한다."""
    redis = _CapturingRedis()
    await _flusher(redis)._publish_thresholds([_entry(threshold_triggered=80)])
    assert len(redis.published) == 1
    payload = redis.published[0][1]["payload"]
    assert payload["threshold_pct"] == 80
    assert payload["target_type"] == "user"


async def test_the_envelope_keeps_the_payload_wrapper():
    """⚠️ 평평하게 되돌리면 notification-worker 가 "payload Field required" 로 전량 폐기한다.

    그 사고로 예산 80% 경고가 한 번도 발송되지 않은 기간이 있었다.
    """
    redis = _CapturingRedis()
    await _flusher(redis)._publish_thresholds(
        [_entry(threshold_events=[_ev("user", 80)])]
    )
    channel, event = redis.published[0]
    assert channel == "notifications:budget"
    assert set(event) == {"event_id", "type", "timestamp", "source", "payload"}, set(event)
    assert event["type"] == "budget_threshold"


async def test_one_bad_event_does_not_suppress_the_others():
    """발행 실패는 건별로 격리돼야 한다 — 한 건 때문에 나머지를 잃으면 안 된다."""

    class _FlakyRedis(_CapturingRedis):
        async def publish(self, channel, payload):
            data = json.loads(payload)
            if data["payload"]["threshold_pct"] == 90:
                raise RuntimeError("redis hiccup")
            await super().publish(channel, payload)

    redis = _FlakyRedis()
    await _flusher(redis)._publish_thresholds(
        [_entry(threshold_events=[_ev("user", 80), _ev("user", 90), _ev("user", 100)])]
    )
    pcts = [p["payload"]["threshold_pct"] for _c, p in redis.published]
    assert pcts == [80, 100], f"한 건 실패가 다른 건을 삼켰다: {pcts}"


def test_the_schema_allows_multiple_scoped_events():
    assert "threshold_events" in CostStreamEntry.model_fields
    assert CostStreamEntry.model_fields["threshold_events"].is_required() is False
    with pytest.raises(Exception):
        ThresholdEvent(scope="department", threshold_pct=80, used_usd="1", limit_usd="2")
