import json

import pytest

from orchestrator.mcp_parsing import (
    parse_schema_search,
    parse_sql_result,
)


def test_parse_sql_result_ok_from_dict():
    r = parse_sql_result(
        {
            "status": "ok",
            "columns": ["region", "revenue"],
            "rows": [["서울", 1000], ["부산", 500]],
            "row_count": 2,
            "truncated": False,
        }
    )
    assert r.ok
    assert not r.needs_correction
    assert r.columns == ["region", "revenue"]
    assert r.rows == [["서울", 1000], ["부산", 500]]
    assert r.row_count == 2


def test_parse_sql_result_ok_from_json_string():
    payload = json.dumps({"status": "ok", "columns": ["a"], "rows": [[1]]})
    r = parse_sql_result(payload)
    assert r.ok
    assert r.row_count == 1  # inferred from rows


def test_parse_sql_result_rejected():
    r = parse_sql_result({"status": "rejected", "reason": "DELETE 금지", "rule": "SELECT_ONLY"})
    assert r.status == "rejected"
    assert r.needs_correction
    assert "DELETE 금지" in r.failure_feedback()
    assert "SELECT_ONLY" in r.failure_feedback()


def test_parse_sql_result_error():
    r = parse_sql_result({"status": "error", "message": "relation does not exist"})
    assert r.status == "error"
    assert r.needs_correction
    assert "relation does not exist" in r.failure_feedback()


def test_parse_sql_result_unknown_status_is_error():
    r = parse_sql_result({"status": "weird"})
    assert r.status == "error"
    assert r.needs_correction


def test_parse_sql_result_bad_json_raises():
    with pytest.raises(ValueError, match="JSON 파싱 실패"):
        parse_sql_result("not-json{")


def test_parse_sql_result_non_object_raises():
    with pytest.raises(ValueError, match="객체가 아닙니다"):
        parse_sql_result("[1, 2, 3]")


def test_parse_schema_search():
    r = parse_schema_search(
        {
            "results": [
                {
                    "doc_type": "column",
                    "table": "orders",
                    "column": "total_amount",
                    "description": "주문 총액",
                    "ddl_snippet": "total_amount NUMERIC(14,2)",
                    "score": 0.92,
                },
                {"doc_type": "table", "table": "orders", "description": "주문 헤더"},
            ]
        }
    )
    assert len(r.results) == 2
    assert r.results[0].column == "total_amount"
    assert r.results[0].score == pytest.approx(0.92)
    assert r.results[1].column is None
    assert r.results[1].score == 0.0


def test_parse_schema_search_empty():
    assert parse_schema_search({"results": []}).results == []
    assert parse_schema_search({}).results == []


def test_parse_schema_search_skips_non_dict_items():
    r = parse_schema_search({"results": ["bad", {"doc_type": "table", "table": "t"}]})
    assert len(r.results) == 1
