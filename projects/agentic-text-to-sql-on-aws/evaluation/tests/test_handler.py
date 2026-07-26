"""Lambda handler 계약 테스트 (PASS/FAIL/SKIP/오류 응답)."""

from __future__ import annotations

import json

from evaluation.dataapi import DataApiError
from evaluation.goldset import GoldEntry, GoldsetMatcher
from evaluation.handler import handler

from .fakes import FakeRunner

GOLD_SQL = "SELECT region, SUM(total) FROM t GROUP BY region"
GOLD = GoldEntry(id="gold-001", question="지역별 매출 상위 5개 지역을 알려줘", sql=GOLD_SQL)
MATCHER = GoldsetMatcher([GOLD])


def _event(question="지역별 매출 상위 5개 지역을 알려줘", sql="SELECT 1", status="ok"):
    record = {
        "question": question,
        "sql": sql,
        "status": status,
        "session_id": "s" * 40,
        "version": {"bundle": "default", "agent": "dev"},
    }
    body = "t2sql_query_record " + json.dumps(record, ensure_ascii=False)
    return {
        "schemaVersion": "1.0",
        "evaluatorId": "eval-1",
        "evaluatorName": "agentic_t2sql_execution_accuracy",
        "evaluationLevel": "TRACE",
        "evaluationInput": {"sessionSpans": [{"logs": [{"body": body}]}]},
        "evaluationReferenceInputs": [],
        "evaluationTarget": {"traceIds": ["t-1"], "spanIds": ["s-1"]},
    }


def test_pass_when_result_sets_match():
    pred_sql = "SELECT region, SUM(total) FROM t GROUP BY region ORDER BY 2 DESC"
    runner = FakeRunner(
        {
            GOLD_SQL: (["region", "sum"], [["서울", 100], ["부산", 50]]),
            pred_sql: (["r", "s"], [["부산", "50.00"], ["서울", "100.0"]]),
        }
    )
    out = handler(_event(sql=pred_sql), matcher=MATCHER, runner=runner)
    assert out["label"] == "PASS"
    assert out["value"] == 1.0
    assert "EX evaluator v1.0.0" in out["explanation"]
    assert "gold-001" in out["explanation"]
    assert "agent=dev" in out["explanation"]


def test_fail_when_result_sets_differ():
    pred_sql = "SELECT region FROM t"
    runner = FakeRunner(
        {
            GOLD_SQL: (["region", "sum"], [["서울", 100]]),
            pred_sql: (["region"], [["서울"]]),
        }
    )
    out = handler(_event(sql=pred_sql), matcher=MATCHER, runner=runner)
    assert out["label"] == "FAIL"
    assert out["value"] == 0.0
    assert "컬럼 개수" in out["explanation"]


def test_fail_when_predicted_sql_execution_errors():
    pred_sql = "SELECT bogus FROM t"
    runner = FakeRunner(
        {
            GOLD_SQL: (["region"], [["서울"]]),
            pred_sql: DataApiError("column bogus does not exist"),
        }
    )
    out = handler(_event(sql=pred_sql), matcher=MATCHER, runner=runner)
    assert out["label"] == "FAIL"
    assert out["value"] == 0.0
    assert "실행 실패" in out["explanation"]


def test_fail_when_predicted_sql_unsafe():
    pred_sql = "SELECT 1; DROP TABLE orders"
    runner = FakeRunner({GOLD_SQL: (["region"], [["서울"]])})
    out = handler(_event(sql=pred_sql), matcher=MATCHER, runner=runner)
    assert out["label"] == "FAIL"
    # 안전 검사에서 거부되므로 어떤 SQL 도 실행되지 않는다.
    assert runner.executed == []
    assert "안전 검사" in out["explanation"]


def test_fail_when_sql_missing_but_goldset_matched():
    runner = FakeRunner({})
    out = handler(_event(sql=None, status="error"), matcher=MATCHER, runner=runner)
    assert out["label"] == "FAIL"
    assert "생성 SQL 이 없습니다" in out["explanation"]


def test_skip_when_no_goldset_match():
    runner = FakeRunner({})
    out = handler(_event(question="어제 날씨 알려줘"), matcher=MATCHER, runner=runner)
    assert out["label"] == "SKIP"
    assert "value" not in out
    assert runner.executed == []


def test_skip_when_question_missing():
    event = {"evaluationInput": {"sessionSpans": [{"body": "관계없는 로그"}]}}
    out = handler(event, matcher=MATCHER, runner=FakeRunner({}))
    assert out["label"] == "SKIP"
    assert "질문을 추출할 수 없어" in out["explanation"]


def test_skip_when_clarification_run():
    out = handler(
        _event(sql=None, status="clarification"), matcher=MATCHER, runner=FakeRunner({})
    )
    assert out["label"] == "SKIP"
    assert "clarification" in out["explanation"]


def test_error_when_evaluation_input_missing():
    out = handler({"schemaVersion": "1.0"}, matcher=MATCHER, runner=FakeRunner({}))
    assert out["errorCode"] == "InvalidEvaluationInput"
    assert "evaluationInput" in out["errorMessage"]


def test_error_when_gold_sql_fails():
    pred_sql = "SELECT 1"
    runner = FakeRunner(
        {
            GOLD_SQL: DataApiError("relation t does not exist"),
            pred_sql: (["a"], [[1]]),
        }
    )
    out = handler(_event(sql=pred_sql), matcher=MATCHER, runner=runner)
    assert out["errorCode"] == "DataSourceExecutionError"
    assert "gold SQL 실행 실패" in out["errorMessage"]


def test_error_response_on_unexpected_exception():
    class Boom(GoldsetMatcher):
        def __init__(self):
            super().__init__([])

        def match(self, question):
            raise RuntimeError("터짐")

    out = handler(_event(), matcher=Boom(), runner=FakeRunner({}))
    assert out["errorCode"] == "InternalEvaluatorError"
    assert "터짐" in out["errorMessage"]


def test_evaluator_version_env_override(monkeypatch):
    monkeypatch.setenv("EVALUATOR_VERSION", "9.9.9")
    out = handler(_event(question="없는 질문"), matcher=MATCHER, runner=FakeRunner({}))
    assert "EX evaluator v9.9.9" in out["explanation"]


def test_skip_when_goldset_datasource_not_aurora():
    matcher = GoldsetMatcher(
        [GoldEntry(id="g-rs", question="레드시프트 질문", sql="SELECT 1", datasource="redshift")]
    )
    out = handler(_event(question="레드시프트 질문"), matcher=matcher, runner=FakeRunner({}))
    assert out["label"] == "SKIP"
    assert "redshift" in out["explanation"]
