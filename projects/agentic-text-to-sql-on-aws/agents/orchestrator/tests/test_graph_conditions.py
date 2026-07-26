"""agent_builder 의 Graph 조건 함수(순수 부분) 테스트.

build_graph/_model 은 strands 를 임포트하지만, 조건 함수들은 로컬 모듈만 쓰므로
가짜 state 객체로 단위 테스트가 가능하다.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from orchestrator.agent_builder import (
    _execution_succeeded,
    _last_execution_failed,
    execution_needs_retry,
    make_retry_condition,
)


def _execution_needs_retry(state):
    return execution_needs_retry(state, state.max_sql_corrections)


@dataclass
class FakeNodeResult:
    result: Any


@dataclass
class FakeState:
    results: dict = field(default_factory=dict)
    execution_order: list = field(default_factory=list)
    max_sql_corrections: int = 3


def _state_with_execution(text: str, exec_count: int = 1, max_corr: int = 3):
    return FakeState(
        results={"execution": FakeNodeResult(result=text)},
        execution_order=["intent", "schema_linking", "sql_generation"]
        + ["execution"] * exec_count,
        max_sql_corrections=max_corr,
    )


def test_ok_result_not_failed():
    st = _state_with_execution(json.dumps({"status": "ok", "rows": [[1]]}))
    assert _last_execution_failed(st) is False
    assert _execution_succeeded(st) is True
    assert _execution_needs_retry(st) is False


def test_rejected_result_failed_and_retries():
    st = _state_with_execution(
        json.dumps({"status": "rejected", "reason": "no", "rule": "SELECT_ONLY"}),
        exec_count=1,
    )
    assert _last_execution_failed(st) is True
    assert _execution_succeeded(st) is False
    assert _execution_needs_retry(st) is True


def test_error_result_retries_within_budget():
    st = _state_with_execution(json.dumps({"status": "error", "message": "x"}), exec_count=2)
    assert _execution_needs_retry(st) is True


def test_no_retry_when_budget_exhausted():
    # execution 이 4번 실행됨(max_corrections=3 → 총 4회 허용, 다음은 없음)
    st = _state_with_execution(
        json.dumps({"status": "error", "message": "x"}), exec_count=4, max_corr=3
    )
    assert _execution_needs_retry(st) is False


def test_missing_execution_result_not_failed():
    assert _last_execution_failed(FakeState()) is False
    assert _execution_succeeded(FakeState()) is True


def test_plain_text_error_signal_detected():
    st = _state_with_execution("The query was rejected due to policy")
    assert _last_execution_failed(st) is True


def test_make_retry_condition_captures_budget():
    cond = make_retry_condition(1)
    # 1회 실패 → 재시도 가능(예산 1)
    st1 = _state_with_execution(json.dumps({"status": "error", "message": "x"}), exec_count=1)
    assert cond(st1) is True
    # 2회 실패 → 예산 소진(max=1 이므로 2번째 시도가 마지막, 재시도 없음)
    st2 = _state_with_execution(json.dumps({"status": "error", "message": "x"}), exec_count=2)
    assert cond(st2) is False
