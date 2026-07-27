"""스팬 파싱(t2sql_query_record / gen_ai 속성 폴백) 테스트."""

from __future__ import annotations

import json

from evaluation.spans import extract_run


def _record(**overrides):
    record = {
        "question": "지역별 매출 상위 5개 지역을 알려줘",
        "sql": "SELECT region FROM customers",
        "status": "ok",
        "session_id": "sess-1234567890123456789012345678901234",
        "version": {"bundle": "b-1@v-2", "agent": "sha-abc"},
    }
    record.update(overrides)
    return record


def test_extract_from_query_record_log_body():
    body = "INFO orchestrator t2sql_query_record " + json.dumps(
        _record(), ensure_ascii=False
    )
    run = extract_run({"sessionSpans": [{"logs": [{"body": body}]}]})
    assert run.from_query_record is True
    assert run.question.startswith("지역별 매출")
    assert run.sql == "SELECT region FROM customers"
    assert run.status == "ok"
    assert run.version == {"bundle": "b-1@v-2", "agent": "sha-abc"}


def test_extract_uses_last_query_record():
    first = "t2sql_query_record " + json.dumps(_record(sql="SELECT 1"), ensure_ascii=False)
    second = "t2sql_query_record " + json.dumps(_record(sql="SELECT 2"), ensure_ascii=False)
    run = extract_run({"sessionSpans": [{"body": first}, {"body": second}]})
    assert run.sql == "SELECT 2"


def test_extract_from_nested_attributes_map():
    record = "t2sql_query_record " + json.dumps(_record(), ensure_ascii=False)
    spans = [
        {
            "attributes": {
                "aws.local.service": "orchestrator",
                "log.record": {"message": record},
            }
        }
    ]
    run = extract_run({"sessionSpans": spans})
    assert run.question is not None and run.sql is not None


def test_extract_handles_nested_braces_in_record():
    record = _record(question='질문 {중괄호} "인용" 포함')
    body = "t2sql_query_record " + json.dumps(record, ensure_ascii=False)
    run = extract_run({"sessionSpans": [{"body": body}]})
    assert run.question == '질문 {중괄호} "인용" 포함'


def test_fallback_gen_ai_attributes():
    spans = [
        {
            "attributes": {
                "gen_ai.prompt": "월별 주문 수를 알려줘",
                "gen_ai.tool.input": json.dumps(
                    {"sql": "SELECT count(*) FROM orders", "datasource": "aurora"}
                ),
            }
        }
    ]
    run = extract_run({"sessionSpans": spans})
    assert run.from_query_record is False
    assert run.question == "월별 주문 수를 알려줘"
    assert run.sql == "SELECT count(*) FROM orders"


def test_fallback_otel_key_value_attributes():
    spans = [
        {
            "attributes": [
                {"key": "gen_ai.prompt", "value": {"stringValue": "취소된 주문 건수를 알려줘"}},
                {"key": "db.statement", "value": {"stringValue": "SELECT COUNT(*) FROM orders"}},
            ]
        }
    ]
    run = extract_run({"sessionSpans": spans})
    assert run.question == "취소된 주문 건수를 알려줘"
    assert run.sql == "SELECT COUNT(*) FROM orders"


def test_fallback_message_array_prompt():
    spans = [
        {
            "attributes": {
                "gen_ai.input.messages": json.dumps(
                    [{"role": "user", "content": [{"type": "text", "text": "카테고리별 매출"}]}]
                ),
                "db.query.text": "SELECT 1",
            }
        }
    ]
    run = extract_run({"sessionSpans": spans})
    assert run.question == "카테고리별 매출"


def test_extract_returns_empty_for_unusable_input():
    run = extract_run(None)
    assert run.has_minimum() is False
    assert run.question is None and run.sql is None


def test_query_record_null_sql_falls_back_to_attributes():
    body = "t2sql_query_record " + json.dumps(
        _record(sql=None, status="error"), ensure_ascii=False
    )
    spans = [{"body": body, "attributes": {"db.statement": "SELECT 9"}}]
    run = extract_run({"sessionSpans": spans})
    assert run.status == "error"
    assert run.sql == "SELECT 9"


def test_accepts_raw_span_list():
    body = "t2sql_query_record " + json.dumps(_record(), ensure_ascii=False)
    run = extract_run([{"body": body}])
    assert run.has_minimum() is True
