"""결과셋 정규화 비교 + 경량 SQL 방어 테스트."""

from __future__ import annotations

import pytest

from evaluation.comparison import (
    UnsafeSqlError,
    assert_safe_select,
    compare_result_sets,
    normalize_rows,
    normalize_value,
    strip_sql_noise,
)


def test_compare_ignores_row_order():
    gold = [["서울", 100], ["부산", 50]]
    pred = [["부산", 50], ["서울", 100]]
    assert compare_result_sets(gold, pred).matched is True


def test_compare_ignores_numeric_representation():
    gold = [["서울", "100.00"]]
    pred = [["서울", 100.0000001]]
    assert compare_result_sets(gold, pred).matched is True


def test_compare_detects_duplicate_row_count_difference():
    gold = [["a", 1], ["a", 1]]
    pred = [["a", 1]]
    result = compare_result_sets(gold, pred)
    assert result.matched is False
    assert "행 수" in result.summary


def test_compare_detects_column_count_mismatch():
    result = compare_result_sets([["서울", 1]], [["서울", 1, 2]])
    assert result.matched is False
    assert "컬럼 개수" in result.summary


def test_compare_reports_missing_and_extra():
    result = compare_result_sets([["서울", 1]], [["부산", 2]])
    assert result.matched is False
    assert "누락" in result.summary and "초과" in result.summary


def test_compare_empty_sets_match():
    assert compare_result_sets([], []).matched is True


def test_normalize_value_and_rows():
    assert normalize_value(None) is None
    assert normalize_value("3.1415926535") == 3.141593
    assert normalize_value(" 서울 ") == "서울"
    assert normalize_rows([[1, "a"]]) == normalize_rows([["1.0", "a"]])


def test_assert_safe_select_accepts_select_and_with():
    assert assert_safe_select("SELECT 1;") == "SELECT 1"
    assert assert_safe_select(
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t"
    ).startswith("WITH")


def test_assert_safe_select_rejects_multiple_statements():
    with pytest.raises(UnsafeSqlError):
        assert_safe_select("SELECT 1; DROP TABLE orders")


def test_assert_safe_select_rejects_non_select():
    with pytest.raises(UnsafeSqlError):
        assert_safe_select("DELETE FROM orders")
    with pytest.raises(UnsafeSqlError):
        assert_safe_select("")


def test_assert_safe_select_rejects_write_keyword_in_body():
    with pytest.raises(UnsafeSqlError):
        assert_safe_select("SELECT * FROM orders WHERE 1=1 UNION SELECT 1 INSERT")


def test_assert_safe_select_allows_semicolon_inside_literal():
    sql = "SELECT * FROM orders WHERE status = 'a;b'"
    assert assert_safe_select(sql) == sql


def test_strip_sql_noise_removes_comments_and_literals():
    cleaned = strip_sql_noise("SELECT 1 -- drop table x\n/* delete */ , 'lit;eral'")
    assert "drop" not in cleaned.lower()
    assert "delete" not in cleaned.lower()
    assert "literal" not in cleaned


def test_assert_safe_select_rejects_write_hidden_by_comment_marker():
    # 주석 안의 키워드는 무시되지만, 주석 밖 statement 는 그대로 검사된다.
    assert assert_safe_select("SELECT 1 -- insert\n") == "SELECT 1 -- insert"
