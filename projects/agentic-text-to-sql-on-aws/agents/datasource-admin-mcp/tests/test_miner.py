"""CandidateMiner 단위 테스트 (fake logs client + fake repository, AWS 호출 없음).

§9.4 계약: mine_candidates 는 orchestrator 의 ``t2sql_query_record`` 로그에서
fewshot/term 후보를 candidate 로 적재하고, 이미 있는 entity_id 는 skip 한다.
"""

from __future__ import annotations

import json

import pytest

from datasource_admin_mcp.miner import (
    MAX_REPORTED_CANDIDATES,
    CandidateMiner,
    fewshot_entity_id,
    normalize_question,
    parse_record,
    term_entity_id,
    tokenize,
)

from .fakes import FakeLogsClient, FakeRepository

GROUP = "/aws/bedrock-agentcore/runtimes/agentic_t2sql_orchestrator-ABC/runtime-logs"
OTHER_GROUP = "/aws/bedrock-agentcore/runtimes/agentic_t2sql_sql_execution_mcp-XYZ/runtime-logs"

# 테스트 기준 시각(고정 clock — CLAUDE.md: 시간 의존 테스트는 결정적으로).
NOW = 1_800_000_000.0
NOW_MS = int(NOW * 1000)
HOUR_MS = 3_600_000


def record_line(
    question: str,
    sql: str = "",
    status: str = "ok",
    session_id: str = "sess-1",
    prefix: str = "INFO:orchestrator: ",
    suffix: str = "",
) -> str:
    """orchestrator 가 남기는 형태의 로그 메시지 한 줄."""
    payload = {
        "question": question,
        "sql": sql,
        "status": status,
        "session_id": session_id,
        "version": {"bundle": "default", "agent": "m5-abc"},
    }
    return f"{prefix}t2sql_query_record {json.dumps(payload, ensure_ascii=False)}{suffix}"


def make_miner(
    events: list[tuple[str, int, str]],
    groups: list[str] | None = None,
) -> tuple[CandidateMiner, FakeRepository, FakeLogsClient]:
    repo = FakeRepository()
    logs = FakeLogsClient(
        groups=groups if groups is not None else [GROUP, OTHER_GROUP], events=events
    )
    miner = CandidateMiner(
        repo,
        logs_client=logs,
        log_group_prefix="/aws/bedrock-agentcore/runtimes/",
        clock=lambda: NOW,
    )
    return miner, repo, logs


# --- 레코드 파싱 --------------------------------------------------------------


def test_parse_record_extracts_json_after_marker():
    parsed = parse_record(record_line("월별 매출", "SELECT 1"))
    assert parsed["question"] == "월별 매출"
    assert parsed["sql"] == "SELECT 1"
    assert parsed["status"] == "ok"
    assert parsed["session_id"] == "sess-1"


def test_parse_record_tolerates_trailing_noise():
    # 마커 뒤 첫 { 부터 raw_decode — 뒤에 붙은 잡음은 무시된다.
    line = record_line("질문", "SELECT 1", suffix="  <-- trailing text\n")
    assert parse_record(line)["question"] == "질문"


def test_parse_record_returns_none_without_marker_or_json():
    assert parse_record("INFO: 평범한 로그") is None
    assert parse_record("t2sql_query_record 마커만 있고 JSON 없음") is None
    assert parse_record("t2sql_query_record {깨진 json") is None
    assert parse_record("") is None
    # JSON 이지만 객체가 아니면 무시.
    assert parse_record('t2sql_query_record ["not", "a", "dict"]') is None


# --- 해시·토크나이저 ----------------------------------------------------------


def test_entity_id_is_stable_and_normalized():
    assert fewshot_entity_id("월별 매출") == fewshot_entity_id("  월별   매출 ")
    assert fewshot_entity_id("Top Customers") == fewshot_entity_id("top customers")
    assert fewshot_entity_id("월별 매출").startswith("mined-")
    assert len(fewshot_entity_id("x")) == len("mined-") + 12
    assert term_entity_id("이탈률").startswith("mined-term-")


def test_normalize_question_collapses_whitespace():
    assert normalize_question(" A \n B\tC ") == "a b c"


def test_tokenize_drops_stopwords_punctuation_and_short_tokens():
    tokens = tokenize("우리 이탈률이 얼마나 되나요? VIP 고객 a 1")
    assert "이탈률이" in tokens
    assert "vip" in tokens
    assert "고객" in tokens
    # 불용어("우리","얼마나")·1자("a")·숫자("1")는 제외.
    for dropped in ("우리", "얼마나", "a", "1"):
        assert dropped not in tokens


# --- 로그 그룹 탐색 -----------------------------------------------------------


def test_discover_log_groups_filters_orchestrator_only():
    miner, _, logs = make_miner([])
    assert miner.discover_log_groups() == [GROUP]
    assert logs.describe_calls[0]["logGroupNamePrefix"] == "/aws/bedrock-agentcore/runtimes/"


def test_discover_log_groups_handles_pagination():
    repo = FakeRepository()

    class Paging(FakeLogsClient):
        def describe_log_groups(self, **kwargs):
            self.describe_calls.append(kwargs)
            if "nextToken" not in kwargs:
                return {"logGroups": [{"logGroupName": GROUP}], "nextToken": "t1"}
            return {"logGroups": [{"logGroupName": GROUP + "-2"}]}

    miner = CandidateMiner(repo, logs_client=Paging(), clock=lambda: NOW)
    assert miner.discover_log_groups() == [GROUP, GROUP + "-2"]


def test_no_orchestrator_group_yields_empty_result():
    miner, repo, _ = make_miner([], groups=[OTHER_GROUP])
    result = miner.mine()
    assert result == {"scanned": 0, "mined": 0, "skipped_existing": 0, "candidates": []}
    assert repo.puts == []


# --- fewshot 채굴 -------------------------------------------------------------


def test_mines_fewshot_from_successful_records():
    events = [
        (GROUP, NOW_MS, record_line("월별 매출 추이", "SELECT 1", "ok", "sess-9")),
    ]
    miner, repo, _ = make_miner(events)
    result = miner.mine()

    assert result["scanned"] == 1
    assert result["mined"] == 1
    assert result["skipped_existing"] == 0
    assert result["candidates"] == [
        {"entity_type": "fewshot", "entity_id": fewshot_entity_id("월별 매출 추이")}
    ]

    stored = repo.get_entity("fewshot", fewshot_entity_id("월별 매출 추이"))
    assert stored["status"] == "candidate"
    assert stored["question"] == "월별 매출 추이"
    assert stored["sql"] == "SELECT 1"
    assert stored["source"] == "mined"
    assert stored["mined_from_session"] == "sess-9"


def test_fewshot_requires_status_ok_and_sql():
    events = [
        (GROUP, NOW_MS, record_line("실패 질의", "SELECT 1", "error")),
        (GROUP, NOW_MS, record_line("SQL 없는 성공", "", "ok")),
        (GROUP, NOW_MS, record_line("   ", "SELECT 1", "ok")),
    ]
    miner, repo, _ = make_miner(events)
    result = miner.mine()
    assert result["scanned"] == 3
    assert [p for p in repo.puts if p["entity_type"] == "fewshot"] == []


def test_duplicate_questions_in_same_batch_mined_once():
    events = [
        (GROUP, NOW_MS, record_line("월별 매출", "SELECT 1")),
        (GROUP, NOW_MS, record_line("월별  매출", "SELECT 2")),  # 정규화하면 같은 질문
    ]
    miner, repo, _ = make_miner(events)
    result = miner.mine()
    assert result["scanned"] == 2
    assert result["mined"] == 1
    assert len(repo.puts) == 1


def test_actor_is_recorded_for_audit():
    events = [(GROUP, NOW_MS, record_line("질문", "SELECT 1"))]
    miner, repo, _ = make_miner(events)
    miner.mine(actor="manager@example.com")
    entity = repo.get_entity("fewshot", fewshot_entity_id("질문"))
    assert entity["updated_by"] == "manager@example.com"


# --- term 채굴 ----------------------------------------------------------------


def test_mines_term_from_repeated_tokens_in_failed_questions():
    events = [
        (GROUP, NOW_MS, record_line("이탈률 알려줘", "", "error")),
        (GROUP, NOW_MS, record_line("이탈률 추이", "", "clarification")),
        (GROUP, NOW_MS, record_line("객단가 알려줘", "", "error")),  # 1회만 → 제외
    ]
    miner, repo, _ = make_miner(events)
    result = miner.mine()

    term_ids = {p["entity_id"] for p in repo.puts if p["entity_type"] == "term"}
    assert term_entity_id("이탈률") in term_ids
    assert term_entity_id("객단가") not in term_ids

    entity = repo.get_entity("term", term_entity_id("이탈률"))
    assert entity["term"] == "이탈률"
    assert entity["status"] == "candidate"
    assert entity["synonyms"] == []
    assert entity["source"] == "mined"
    assert "정의 필요" in entity["definition"]
    assert result["mined"] >= 1


def test_successful_questions_do_not_produce_terms():
    events = [
        (GROUP, NOW_MS, record_line("이탈률 알려줘", "SELECT 1", "ok")),
        (GROUP, NOW_MS, record_line("이탈률 추이", "SELECT 2", "ok")),
    ]
    miner, repo, _ = make_miner(events)
    miner.mine()
    assert [p for p in repo.puts if p["entity_type"] == "term"] == []


def test_repeated_token_within_one_question_counts_once():
    events = [(GROUP, NOW_MS, record_line("이탈률 이탈률 이탈률", "", "error"))]
    miner, repo, _ = make_miner(events)
    miner.mine()
    assert [p for p in repo.puts if p["entity_type"] == "term"] == []


# --- 중복 채굴 방지 -----------------------------------------------------------


def test_existing_entity_is_skipped_regardless_of_status():
    events = [(GROUP, NOW_MS, record_line("월별 매출", "SELECT 1"))]
    miner, repo, _ = make_miner(events)

    first = miner.mine()
    assert (first["mined"], first["skipped_existing"]) == (1, 0)

    second = miner.mine()
    assert (second["mined"], second["skipped_existing"]) == (0, 1)
    assert len(repo.puts) == 1


def test_rejected_candidate_is_not_re_mined():
    """반려한 후보가 배치 재실행으로 되살아나지 않아야 한다(§9.1)."""
    events = [(GROUP, NOW_MS, record_line("월별 매출", "SELECT 1"))]
    miner, repo, _ = make_miner(events)
    miner.mine()
    entity_id = fewshot_entity_id("월별 매출")
    repo.reject("fewshot", entity_id, reason="품질 미달")

    result = miner.mine()
    assert (result["mined"], result["skipped_existing"]) == (0, 1)
    assert repo.get_entity("fewshot", entity_id)["status"] == "rejected"


def test_published_candidate_is_not_re_mined():
    events = [(GROUP, NOW_MS, record_line("월별 매출", "SELECT 1"))]
    miner, repo, _ = make_miner(events)
    miner.mine()
    repo.publish("fewshot", fewshot_entity_id("월별 매출"))
    result = miner.mine()
    assert (result["mined"], result["skipped_existing"]) == (0, 1)


# --- hours 윈도우 -------------------------------------------------------------


def test_hours_window_sets_start_time():
    events = [(GROUP, NOW_MS, record_line("질문", "SELECT 1"))]
    miner, _, logs = make_miner(events)
    miner.mine(hours=6)
    assert logs.filter_calls[0]["startTime"] == NOW_MS - 6 * HOUR_MS
    assert logs.filter_calls[0]["filterPattern"] == '"t2sql_query_record"'


def test_events_outside_window_excluded():
    events = [
        (GROUP, NOW_MS - 30 * HOUR_MS, record_line("오래된 질의", "SELECT 1")),
        (GROUP, NOW_MS - 1 * HOUR_MS, record_line("최근 질의", "SELECT 2")),
    ]
    miner, repo, _ = make_miner(events)
    result = miner.mine(hours=24)
    assert result["scanned"] == 1
    assert repo.puts[0]["entity_id"] == fewshot_entity_id("최근 질의")


def test_invalid_hours_rejected():
    miner, _, _ = make_miner([])
    for bad in (0, -1, 1.5, "24", True):
        with pytest.raises(ValueError):
            miner.mine(hours=bad)


# --- 페이지네이션·복원력 -------------------------------------------------------


def test_filter_log_events_pagination_followed():
    repo = FakeRepository()

    class Paging(FakeLogsClient):
        def filter_log_events(self, **kwargs):
            self.filter_calls.append(kwargs)
            if "nextToken" not in kwargs:
                return {
                    "events": [{"message": record_line("질문1", "SELECT 1")}],
                    "nextToken": "p2",
                }
            return {"events": [{"message": record_line("질문2", "SELECT 2")}]}

    miner = CandidateMiner(repo, logs_client=Paging(groups=[GROUP]), clock=lambda: NOW)
    result = miner.mine()
    assert result["scanned"] == 2
    assert result["mined"] == 2


def test_filter_log_events_failure_skips_group_without_raising():
    repo = FakeRepository()

    class Boom(FakeLogsClient):
        def filter_log_events(self, **kwargs):
            raise RuntimeError("AccessDenied")

    miner = CandidateMiner(repo, logs_client=Boom(groups=[GROUP]), clock=lambda: NOW)
    assert miner.mine()["scanned"] == 0


def test_candidates_list_truncated_but_counts_complete():
    # 후보 60건 → 응답 목록은 50건까지, mined 카운트는 60.
    events = [
        (GROUP, NOW_MS, record_line(f"질문 {i}", f"SELECT {i}")) for i in range(60)
    ]
    miner, _, _ = make_miner(events)
    result = miner.mine()
    assert result["mined"] == 60
    # miner 자체는 전체를 반환하고, 절단은 server 도구 계층이 수행한다.
    assert len(result["candidates"]) == 60
    assert MAX_REPORTED_CANDIDATES == 50
