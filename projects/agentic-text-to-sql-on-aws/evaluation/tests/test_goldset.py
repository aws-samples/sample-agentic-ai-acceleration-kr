"""goldset 로딩·질문 매칭 테스트."""

from __future__ import annotations

from evaluation.goldset import (
    DEFAULT_GOLDSET_PATH,
    GoldEntry,
    GoldsetMatcher,
    load_goldset,
    normalize_question,
)


def test_bundled_goldset_loads():
    entries = load_goldset()
    assert DEFAULT_GOLDSET_PATH.exists()
    # §9.3: 5문항 이상, aurora 만.
    assert len(entries) >= 5
    assert all(e.datasource == "aurora" for e in entries)
    assert all(e.sql.strip().lower().startswith(("select", "with")) for e in entries)
    ids = [e.id for e in entries]
    assert len(ids) == len(set(ids))


def test_bundled_goldset_contains_e2e_default_question():
    matcher = GoldsetMatcher()
    hit = matcher.match("지역별 매출 상위 5개 지역을 알려줘")
    assert hit is not None
    assert "orders" in hit.sql.lower()


def test_normalize_question_removes_punctuation_and_space():
    assert normalize_question("  지역별 매출, 상위 5개?! ") == "지역별매출상위5개"
    assert normalize_question("Top 5 REGIONS!") == "top5regions"


def test_match_exact_ignores_formatting():
    entries = [GoldEntry(id="g1", question="월별 주문 수를 알려줘", sql="SELECT 1")]
    matcher = GoldsetMatcher(entries)
    assert matcher.match("월별  주문 수를 알려줘!!").id == "g1"


def test_match_partial_prefers_longest():
    entries = [
        GoldEntry(id="short", question="매출", sql="SELECT 1"),
        GoldEntry(id="long", question="지역별 매출 상위", sql="SELECT 2"),
    ]
    matcher = GoldsetMatcher(entries)
    hit = matcher.match("지역별 매출 상위 5개 지역을 알려줘")
    assert hit is not None and hit.id == "long"


def test_match_returns_none_for_unrelated():
    entries = [GoldEntry(id="g1", question="월별 주문 수", sql="SELECT 1")]
    matcher = GoldsetMatcher(entries)
    assert matcher.match("어제 날씨 어때") is None
    assert matcher.match("") is None


def test_load_goldset_skips_broken_lines(tmp_path):
    path = tmp_path / "goldset-test.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"id":"g1","question":"q1","sql":"SELECT 1","datasource":"aurora"}',
                "not json",
                '{"question":"missing id","sql":"SELECT 2"}',
                "",
                '{"id":"g2","question":"q2","sql":"SELECT 3"}',
            ]
        ),
        encoding="utf-8",
    )
    entries = load_goldset(path)
    assert [e.id for e in entries] == ["g1", "g2"]
    # datasource 기본값은 aurora.
    assert entries[1].datasource == "aurora"
