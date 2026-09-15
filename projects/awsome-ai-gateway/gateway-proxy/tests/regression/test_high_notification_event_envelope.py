# Copyright 2026 Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: 알림 발행자(producer)와 notification-worker(consumer) 계약 일치.

Bug (실사고 2건, 라이브 확인):
  1. gateway-proxy 의 SecurityEventType 이 대문자("AUTH_FAILURE_SPIKE")였는데 worker 의
     EventType 은 소문자("auth_failure_spike")였다 → pydantic 이 거부.
  2. 두 발행자(cost-recorder-worker 예산, gateway-proxy 보안) 모두 도메인 필드를 **평평하게**
     발행했는데 worker 의 NotificationEvent 는 `payload: dict` 를 필수로 요구한다 → 거부.
  두 경우 모두 parse_pubsub_message 가 None 을 반환하고 리스너가 조용히 스킵하므로,
  전체 알림 서브시스템이 아무것도 배달하지 않았다(dev notification_logs 0행 — 예산 80%
  경고도, 브루트포스 감지 경보도 한 번도 나가지 않음).

왜 아무도 못 잡았나: 발행자 테스트는 채널 이름만 검증하고
(cost-recorder-worker/tests/unit/test_batch_flusher.py:134,
gateway-proxy/tests/unit/test_event_detector_bounded.py:73), 소비자 테스트는
**올바른 봉투를 손으로 써서** 넣는다(notification-worker/tests/integration/
test_event_flow.py). 양쪽 다 각자 통과하고 사이가 비어 있었다.

이 테스트는 그 사이를 메운다 — 소비자 모델을 **파일 경로로 실제 로드**해서 발행자 출력을
그대로 먹여본다. 서비스가 서로 다른 파이썬 패키지라 import 로는 닿지 않기 때문이다.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from app.schemas.domain import AuthType, SecurityEvent, SecurityEventType

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # regression -> tests -> gateway-proxy -> root
WORKER_EVENTS = PROJECT_ROOT / "notification-worker" / "src" / "worker" / "schemas" / "events.py"
BATCH_FLUSHER = PROJECT_ROOT / "cost-recorder-worker" / "src" / "worker" / "batch_flusher.py"
SEED_SQL = PROJECT_ROOT / "db" / "init" / "06_seed_notification_configs.sql"

# 소비자가 최상위에 요구하는 필드. 나머지 도메인 필드는 전부 payload 안으로.
ENVELOPE_KEYS = {"event_id", "type", "timestamp", "source", "payload"}


def _load_worker_events():
    """notification-worker 의 events.py 를 파일 경로로 로드한다.

    notification-worker 와 cost-recorder-worker 는 **둘 다** 최상위 패키지 이름이
    `worker` 라서 sys.path 로는 안전하게 import 할 수 없다. 이 모듈은 pydantic 만
    의존하므로 경로 로드로 충분하다.
    """
    if not WORKER_EVENTS.exists():
        pytest.skip(f"{WORKER_EVENTS} 없음 (부분 체크아웃)")
    spec = importlib.util.spec_from_file_location("_nw_events_under_test", WORKER_EVENTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _published_event_keys() -> set[str]:
    """batch_flusher 가 예산 알림으로 발행하는 dict 의 최상위 키를 AST 로 뽑는다.

    실제로 publish 를 호출하려면 cost-recorder-worker 패키지를 import 해야 하는데
    위 이유로 불가능하다. 대신 소스의 dict 리터럴을 읽어 키 집합을 확인한다 —
    누군가 다시 평평하게 되돌리면(= 원래 사고) 키 집합이 달라져 실패한다.

    ⚠️ **특정 함수 이름에 매달지 않는다.** 처음에는 ``_publish_thresholds`` 안만 뒤졌는데,
       스코프별 발행으로 리팩터하면서 리터럴이 헬퍼로 옮겨가자 이 검사가 "리터럴을 찾지
       못했다" 로 실패했다 — 계약은 그대로인데 검사가 위치에 의존한 것이다. 이제 모듈
       전체에서 ``notifications:budget`` 으로 publish 하는 함수를 찾아 그 안의 리터럴을
       읽는다.
    """
    if not BATCH_FLUSHER.exists():
        pytest.skip(f"{BATCH_FLUSHER} 없음 (부분 체크아웃)")
    tree = ast.parse(BATCH_FLUSHER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        publishes_budget = any(
            isinstance(c, ast.Constant) and c.value == "notifications:budget"
            for c in ast.walk(node)
        )
        if not publishes_budget:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Assign)
                and len(inner.targets) == 1
                and isinstance(inner.targets[0], ast.Name)
                and inner.targets[0].id == "event"
                and isinstance(inner.value, ast.Dict)
            ):
                return {
                    k.value
                    for k in inner.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    pytest.fail(
        "notifications:budget 으로 publish 하는 함수에서 `event = {...}` 리터럴을 찾지 못했다"
    )


def _seed_event_types() -> set[str]:
    sql = SEED_SQL.read_text(encoding="utf-8")
    # ('budget_threshold',    '["affected_user", ...]', true),
    return {m.group(1) for m in re.finditer(r"^\s*\('([a-z_]+)'\s*,", sql, flags=re.M)}


def test_security_event_envelope_is_accepted_by_the_consumer():
    """gateway-proxy 가 실제로 발행하는 바이트가 worker 파서를 통과해야 한다."""
    events = _load_worker_events()

    event = SecurityEvent(
        event_id="11111111-2222-3333-4444-555555555555",
        type=SecurityEventType.AUTH_FAILURE_SPIKE,
        timestamp="2026-09-09T01:02:03+00:00",
        source_ip="10.0.0.9",
        failure_count=7,
        window_minutes=5,
        auth_type=AuthType.VIRTUAL_KEY,
        details="7 failures in 300s window",
    )
    # event_detector.py 가 채널에 쓰는 것과 동일한 바이트
    raw = json.dumps(event.to_envelope())

    parsed = events.parse_pubsub_message(raw)
    assert parsed is not None, (
        "worker 가 gateway-proxy 의 보안 이벤트를 파싱하지 못한다 — "
        "봉투(payload) 또는 type 값 규약이 다시 갈라졌다"
    )
    assert parsed.type == events.EventType.AUTH_FAILURE_SPIKE
    assert parsed.source == events.ServiceSource.GATEWAY_PROXY
    # 도메인 필드는 핸들러/recipient_resolver 가 payload 안에서 읽는다
    assert parsed.payload["source_ip"] == "10.0.0.9"
    assert parsed.payload["failure_count"] == 7
    # 봉투 필드가 payload 에 중복으로 남아 있으면 안 된다
    assert not (ENVELOPE_KEYS - {"payload"}) & set(parsed.payload)


def test_every_security_event_type_exists_in_the_consumer_enum():
    """발행 가능한 모든 보안 이벤트 타입이 소비자 enum 에 있어야 한다."""
    events = _load_worker_events()
    consumer = {e.value for e in events.EventType}
    producer = {e.value for e in SecurityEventType}
    missing = sorted(producer - consumer)
    assert not missing, (
        f"worker EventType 에 없는 보안 이벤트 타입: {missing}. "
        "값이 다르면 pydantic 이 거부하고 parse_pubsub_message 가 None 을 반환해 "
        "이벤트가 조용히 사라진다(로그 한 줄, 알림 0건)."
    )


def test_event_types_match_the_db_seed():
    """notification_configs 시드가 tie-breaker — enum 과 정확히 일치해야 한다."""
    events = _load_worker_events()
    consumer = {e.value for e in events.EventType}
    seeded = _seed_event_types()
    assert seeded, f"{SEED_SQL} 에서 event_type 을 못 읽었다 — 시드 형식이 바뀌었는지 확인"
    assert seeded == consumer, (
        f"시드에만 있음={sorted(seeded - consumer)}, enum 에만 있음={sorted(consumer - seeded)}. "
        "recipient_roles 는 event_type 으로 조회되므로 불일치하면 수신자 0명이 된다."
    )


def test_budget_publisher_uses_the_payload_envelope():
    """cost-recorder-worker 예산 이벤트도 같은 봉투를 써야 한다."""
    keys = _published_event_keys()
    assert keys == ENVELOPE_KEYS, (
        f"발행 dict 최상위 키가 {sorted(keys)} — 기대 {sorted(ENVELOPE_KEYS)}. "
        "도메인 필드를 최상위에 평평하게 두면 worker 가 'payload Field required' 로 "
        "전량 폐기한다(예산 경고 미발송 실사고)."
    )


def test_budget_envelope_key_set_validates_against_the_consumer_model():
    """추출한 키 집합만으로 소비자 모델을 실제로 만들어 본다(키 이름 오타까지 잡는다)."""
    events = _load_worker_events()
    keys = _published_event_keys()

    stub = {
        "event_id": "req-80pct",
        "type": "budget_threshold",
        "timestamp": "2026-09-09T01:02:03+00:00",
        "source": "cost-recorder-worker",
        "payload": {"user_id": "u", "team_id": "t", "threshold_pct": 80},
    }
    # 발행자가 실제로 쓰는 키만 남긴다 — 키가 빠졌거나 이름이 틀리면 검증이 실패한다.
    candidate = {k: v for k, v in stub.items() if k in keys}
    parsed = events.NotificationEvent.model_validate(candidate)
    assert parsed.payload["threshold_pct"] == 80
