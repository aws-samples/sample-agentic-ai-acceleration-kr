# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""tests/eval/scoring.py 의 이벤트 추출 + 채점 로직 단위 테스트.

라이브 호출 없이 합성 이벤트 스트림으로 채점기가 올바르게 동작하는지 검증.
이벤트 계약(memory/chat-agent-event-contract)을 코드로 고정한다.
"""

from __future__ import annotations

from tests.eval import scoring


# ─── 합성 이벤트 빌더 ───
def _sql_event(sql: str) -> dict:
    return {"type": "tool_result", "tool": "ask_sql_specialist", "result": {"sql": sql}}


def _code_event(code: str) -> dict:
    return {"type": "tool_result", "tool": "ask_code_specialist", "result": {"code": code}}


def _validator_event(verdict: str, conf: float = 0.9) -> dict:
    return {
        "type": "tool_result",
        "tool": "ask_validator",
        "result": {"verdict": verdict, "confidence": conf},
    }


def _tool_call(name: str) -> dict:
    return {"type": "tool_call", "tool": name, "args": {}}


def _chart(kind: str) -> dict:
    return {"type": "chart", "spec": {"kind": kind, "data": [], "encoding": {}}}


# ─── 추출기 ───
def test_extract_sql_concatenates():
    events = [_sql_event("SELECT 1"), _sql_event("SELECT 2")]
    assert "SELECT 1" in scoring.extract_sql(events)
    assert "SELECT 2" in scoring.extract_sql(events)


def test_extract_agent_path_order():
    events = [_tool_call("ask_sql_specialist"), _tool_call("ask_validator")]
    assert scoring.extract_agent_path(events) == [
        "ask_sql_specialist",
        "ask_validator",
    ]


def test_extract_validator_verdict_last_wins():
    events = [_validator_event("FAIL"), _validator_event("PASS")]
    assert scoring.extract_validator_verdict(events) == "PASS"


def test_extract_chart_kinds():
    events = [_chart("bar"), _chart("line")]
    assert scoring.extract_chart_kinds(events) == ["bar", "line"]


def test_has_error_detects_inband():
    assert scoring.has_error([{"type": "error", "error": "boom"}]) == "boom"
    assert scoring.has_error([{"error_type": "StreamError"}]) == "StreamError"
    assert scoring.has_error([_sql_event("SELECT 1")]) is None


# ─── SQL 체크 ───
def test_required_tables_schema_qualified_and_bare():
    ok, miss = scoring.check_required_tables(
        "SELECT * FROM usage.usage_logs JOIN auth.users", ["usage.usage_logs", "auth.users"]
    )
    assert ok and not miss


def test_required_tables_bare_match():
    # schema 생략 (search_path) 도 허용
    ok, miss = scoring.check_required_tables(
        "SELECT * FROM usage_logs", ["usage.usage_logs"]
    )
    assert ok


def test_required_tables_missing():
    ok, miss = scoring.check_required_tables("SELECT 1", ["usage.usage_logs"])
    assert not ok and "usage.usage_logs" in miss


def test_forbidden_clauses_catches_drift_columns():
    # use_case_07 의 status_code 회귀 가드 시뮬레이션
    ok, hits = scoring.check_forbidden_clauses(
        "SELECT email WHERE status_code = 429", ["status_code"]
    )
    assert not ok and "status_code" in hits


def test_forbidden_clauses_catches_dml():
    ok, hits = scoring.check_forbidden_clauses(
        "DELETE FROM usage_logs", ["INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER"]
    )
    assert not ok


def test_required_clauses_case_insensitive():
    ok, miss = scoring.check_required_clauses(
        "select x group by y order by z desc limit 10",
        ["GROUP BY", "ORDER BY", "DESC", "LIMIT"],
    )
    assert ok and not miss


# ─── §58 KST 앵커 검증 (감사 결함 ③/⑤ 회귀 차단) ───
def test_timezone_kst_passes_when_anchored():
    sql = (
        "SELECT SUM(cost_usd) FROM usage.usage_logs "
        "WHERE requested_at AT TIME ZONE 'Asia/Seoul' >= "
        "date_trunc('month', now() AT TIME ZONE 'Asia/Seoul')"
    )
    ok, _ = scoring.check_timezone_anchored(sql)
    assert ok


def test_timezone_kst_fails_on_raw_utc():
    # 감사 결함③: raw UTC 롤링윈도우 — 9시간 어긋남. 회귀로 잡아야.
    sql = "SELECT SUM(cost_usd) FROM usage.usage_logs WHERE requested_at >= now() - interval '7 days'"
    ok, reason = scoring.check_timezone_anchored(sql)
    assert not ok
    assert "Asia/Seoul" in reason or "KST" in reason


def test_timezone_kst_fails_on_bare_date_trunc_now():
    sql = "SELECT SUM(cost_usd) FROM usage.usage_logs WHERE requested_at >= date_trunc('month', now())"
    ok, _ = scoring.check_timezone_anchored(sql)
    assert not ok


def test_timezone_kst_no_time_filter_passes():
    # 시간 필터가 없으면 해당없음(통과).
    sql = "SELECT email FROM auth.users WHERE is_active = true"
    ok, _ = scoring.check_timezone_anchored(sql)
    assert ok


# ─── code 체크 ───
def test_code_imports_and_technique():
    code = "from sklearn.ensemble import IsolationForest\nimport pandas as pd"
    ok, _ = scoring.check_code_imports(code, ["sklearn"])
    assert ok
    ok2, _ = scoring.check_code_any_of(code, ["IsolationForest", "DBSCAN"])
    assert ok2


def test_code_any_of_miss():
    ok, opts = scoring.check_code_any_of("import numpy", ["IsolationForest"])
    assert not ok and opts == ["IsolationForest"]


# ─── agent path ───
def test_agent_path_excludes():
    ok, hits = scoring.check_agent_path_excludes(
        ["ask_sql_specialist", "ask_code_specialist"], ["ask_code_specialist"]
    )
    assert not ok and "ask_code_specialist" in hits


# ─── chart kind 관대 처리 ───
def test_chart_kind_no_chart_but_table_allowed():
    # 차트 미발행이어도 table/kpi 가 allowed 면 통과
    assert scoring.check_chart_kind([], ["table", "kpi"])
    # 차트 미발행 + bar 만 요구 → 불통과
    assert not scoring.check_chart_kind([], ["bar"])


# ─── 통합 score_case ───
def test_score_case_tier_a_all_pass():
    case = {
        "use_case_id": "01",
        "tier": "A",
        "name": "top users",
        "expected": {
            "sql": {
                "required_tables": ["usage.usage_logs", "auth.users"],
                "required_clauses": ["GROUP BY", "ORDER BY", "DESC", "LIMIT"],
                "forbidden_clauses": ["INSERT|UPDATE|DELETE|DROP"],
            },
            "validator": {"verdict": "PASS", "min_confidence": 0.7},
            "chart": {"kind": ["bar", "table"]},
            "agent_path_includes": ["ask_sql_specialist", "ask_validator"],
            "agent_path_excludes": ["ask_code_specialist"],
        },
    }
    events = [
        {"type": "thinking", "text": "…"},
        _tool_call("ask_sql_specialist"),
        _sql_event(
            "SELECT u.email, SUM(l.cost_usd) FROM usage.usage_logs l "
            "JOIN auth.users u ON u.id=l.user_id GROUP BY u.email "
            "ORDER BY 2 DESC LIMIT 10"
        ),
        _tool_call("ask_validator"),
        _validator_event("PASS", 0.9),
        _chart("bar"),
        {"type": "done"},
    ]
    result = scoring.score_case(case, events)
    assert result["passed"], result
    assert result["pass_rate"] == 1.0


def test_score_case_warn_verdict_is_acceptable_for_pass_expectation():
    case = {
        "use_case_id": "x",
        "tier": "A",
        "expected": {"validator": {"verdict": "PASS"}},
    }
    events = [_validator_event("WARN", 0.6)]
    result = scoring.score_case(case, events)
    # PASS 기대인데 WARN 관측 → 관대 통과 (WARN 은 유효 답변)
    assert result["checks"]["validator_verdict"]


def test_score_case_stream_error_short_circuits():
    case = {"use_case_id": "x", "tier": "A", "expected": {"sql": {}}}
    events = [{"type": "error", "error": "AccessDenied"}]
    result = scoring.score_case(case, events)
    assert not result["passed"]
    assert result["error"] == "AccessDenied"


# ─── 리뷰 발견 회귀 가드 ───
def test_extractors_survive_malformed_events():
    """non-dict 이벤트 / result 가 list / spec 가 list 여도 크래시 안 함."""
    bad = [
        None,
        "raw string",
        42,
        {"type": "tool_result", "tool": "ask_sql_specialist", "result": ["not", "dict"]},
        {"type": "chart", "spec": ["not", "dict"]},
        {"type": "tool_call", "tool": "ask_sql_specialist"},
    ]
    # 어느 추출기도 예외를 던지면 안 됨
    assert scoring.extract_agent_path(bad) == ["ask_sql_specialist"]
    assert scoring.extract_sql(bad) == ""
    assert scoring.extract_chart_kinds(bad) == []
    assert scoring.has_error(bad) is None


def test_validator_absent_fails_when_expected():
    """expected.validator 있는데 validator 미실행(verdict None) → 불통과.

    리뷰 발견: 예전엔 None 을 관대 통과시켜 validator 를 통째로 건너뛴
    케이스가 silently pass 했음.
    """
    case = {
        "use_case_id": "x",
        "tier": "B",
        "expected": {"validator": {"verdict": "PASS", "min_confidence": 0.5}},
    }
    # validator 이벤트 없음 (sql/code 만)
    events = [_sql_event("SELECT 1"), _code_event("import sklearn")]
    result = scoring.score_case(case, events)
    assert result["checks"]["validator_verdict"] is False


def test_validator_fail_verdict_not_accepted_for_pass_expectation():
    case = {"use_case_id": "x", "tier": "A", "expected": {"validator": {"verdict": "PASS"}}}
    events = [_validator_event("FAIL", 0.9)]
    result = scoring.score_case(case, events)
    assert result["checks"]["validator_verdict"] is False


def test_required_clauses_preserves_regex_metachars():
    """패턴 lower() 로 \\D→\\d 뒤집히던 버그 회귀 가드.

    \\D (non-digit) 패턴이 숫자만 있는 곳엔 매치 안 돼야. lower() 했다면
    \\d 로 바뀌어 숫자에 매치돼 결과가 뒤집힘.
    """
    # "ABC" 에는 \D(비숫자)가 매치되어야, "123" 에는 매치 안 되어야
    ok, _ = scoring.check_required_clauses("ABC", [r"\D"])
    assert ok
    ok2, _ = scoring.check_required_clauses("123", [r"\D"])
    assert not ok2


def test_required_clauses_alternation_interval_or_datearith():
    """case 02/04 false-negative 수정: ::date - 30 도 통과해야."""
    pattern = [r"(interval|date_trunc|::date\s*-|now\(\))"]
    # INTERVAL 리터럴
    ok1, _ = scoring.check_required_clauses(
        "WHERE created_at >= now() - interval '30 days'", pattern
    )
    assert ok1
    # ::date - 30 산술 (INTERVAL 리터럴 없음)
    ok2, _ = scoring.check_required_clauses(
        "WHERE d >= (now() at time zone 'asia/seoul')::date - 30", pattern
    )
    assert ok2


# ─── Tier B code envelope fallback (DEVLOG §32) ───
def _code_event_summary(summary: str, code: str = "") -> dict:
    r = {"result_summary": summary}
    if code:
        r["code"] = code
    return r


def test_tier_b_code_present_via_code():
    case = {"use_case_id": "11", "tier": "B",
            "expected": {"code": {"required_any_of": ["SARIMAX"]}}}
    events = [{"type": "tool_result", "tool": "ask_code_specialist",
               "result": {"code": "from statsmodels...SARIMAX(...)"}}]
    r = scoring.score_case(case, events)
    assert r["checks"]["code_present"]
    assert r["checks"]["code_technique"]


def test_tier_b_fallback_to_summary_when_code_empty():
    """code envelope 비어도 result_summary 의 기법 흔적으로 통과 (실 케이스)."""
    case = {"use_case_id": "11", "tier": "B",
            "expected": {"code": {"required_imports": ["statsmodels"],
                                  "required_any_of": ["SARIMAX"]}}}
    events = [{"type": "tool_result", "tool": "ask_code_specialist",
               "result": {"result_summary": "SARIMAX(1,1,1)(1,1,1,7) 모델로 예측 합계 $6,494",
                          "data": {"x": 1}}}]  # code 없음
    r = scoring.score_case(case, events)
    assert r["checks"]["code_present"], "호출+summary 면 code_present 통과해야"
    assert r["checks"]["code_technique"], "summary 의 SARIMAX 로 기법 통과해야"
    assert r["checks"]["code_imports"], "code 부재 시 import 는 관대 통과"


def test_tier_b_fails_when_code_specialist_not_called():
    case = {"use_case_id": "11", "tier": "B",
            "expected": {"code": {"required_any_of": ["SARIMAX"]}}}
    events = [_sql_event("SELECT 1")]  # code specialist 미호출
    r = scoring.score_case(case, events)
    assert not r["checks"]["code_present"]


def test_tier_b_technique_mismatch_in_summary():
    case = {"use_case_id": "11", "tier": "B",
            "expected": {"code": {"required_any_of": ["SARIMAX"]}}}
    events = [{"type": "tool_result", "tool": "ask_code_specialist",
               "result": {"result_summary": "단순 이동평균으로 추정"}}]  # SARIMAX 없음
    r = scoring.score_case(case, events)
    assert r["checks"]["code_present"]   # 호출은 됨
    assert not r["checks"]["code_technique"]  # 기법 흔적 없음


def test_aggregate_by_tier():
    results = [
        {"tier": "A", "passed": True, "pass_rate": 1.0},
        {"tier": "A", "passed": False, "pass_rate": 0.5},
        {"tier": "B", "passed": True, "pass_rate": 1.0},
    ]
    summ = scoring.aggregate(results)
    assert summ["total"] == 3
    assert summ["passed"] == 2
    assert summ["by_tier"]["A"]["passed"] == 1
    assert summ["by_tier"]["B"]["passed"] == 1
