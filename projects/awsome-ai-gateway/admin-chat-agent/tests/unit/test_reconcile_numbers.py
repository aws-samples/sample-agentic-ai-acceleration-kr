# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""_reconcile_numbers 회귀 테스트 — harness reconciliation gate.

DEVLOG §28.6 item 1: 시간 표현("지난 30일")의 숫자가 오탐 WARN 을 유발하던
문제를 시간단위 필터로 해결. 이 테스트가 그 수정을 고정한다.

reconciliation gate 는 fail-soft (WARN 만, 차단 안 함). 핵심 불변식:
  - 실행 결과(envelope)에서 유래한 숫자 → WARN 없음
  - 시간 표현 숫자(30일/90 days) → reconciliation 대상에서 제외 (오탐 방지)
  - 미근거 비-시간 숫자 → WARN 유지 (과잉 억제 방지)
"""

from __future__ import annotations

import pytest


@pytest.fixture
def reconcile(agent_pure_fns):
    return agent_pure_fns["_reconcile_numbers"]


def _sql_env(**nums) -> list[dict]:
    """tool_result 형태의 SQL envelope 헬퍼."""
    return [{"tool": "ask_sql_specialist", "result": {"rows": [nums], **nums}}]


# ─── 시간 표현 오탐 제거 (핵심 수정) ───
@pytest.mark.parametrize(
    "text,tool_results,expect_warn,desc",
    [
        (
            "지난 30일 동안 총 요청은 1,234건입니다.",
            _sql_env(count=1234),
            False,
            "'지난 30일' 의 30 은 시간단위라 제외, 1234 는 근거됨 → WARN 없음",
        ),
        (
            "최근 90 days 추세를 분석했습니다.",
            [],
            False,
            "'90 days' 만 있고 다른 숫자 없음 → 시간 숫자뿐이라 WARN 없음",
        ),
        (
            "3개월째 증가 중이며 평균 비용은 $12.50 입니다.",
            _sql_env(avg=12.5),
            False,
            "'3개월째'(연속접미 째) 제외 + 12.50 근거 → WARN 없음",
        ),
        (
            "24시간 내 호출이 567건 있었습니다.",
            _sql_env(calls=567),
            False,
            "'24시간'·'내' 제외 + 567 근거 → WARN 없음",
        ),
        # 영어 m-시작 단위 회귀 가드 (months/minutes/mins 의 m 이 'million'
        # 배수로 먹히던 버그). 30months 가 30,000,000 으로 부풀어 오탐 WARN 을
        # 내던 케이스 — 시간단위로 인식돼 제외돼야 한다.
        (
            "지난 30 months 추세입니다.",
            [],
            False,
            "'30 months' → 시간(제외), 다른 숫자 없음 → WARN 없음",
        ),
        (
            "최근 30months 데이터를 봤습니다.",
            [],
            False,
            "'30months'(붙여쓰기) → m 을 million 으로 먹지 않고 시간 제외",
        ),
        (
            "지난 90 minutes 동안 처리했습니다.",
            [],
            False,
            "'90 minutes' → 시간(제외) → WARN 없음",
        ),
        (
            "최근 15 mins 추이.",
            [],
            False,
            "'15 mins' → 시간(제외) → WARN 없음",
        ),
    ],
)
def test_time_expression_not_flagged(reconcile, text, tool_results, expect_warn, desc):
    result = reconcile(text, tool_results)
    assert (result is not None) == expect_warn, f"{desc} | got={result}"


def test_english_m_unit_does_not_inflate_other_numbers(reconcile):
    """'3 months ... $9999' 에서 3 은 시간(제외), 9999 만 미근거로 잡혀야.

    버그였다면 3months 가 3,000,000 으로 부풀어 phantom 후보가 생김.
    근거된 숫자(1234)를 함께 둬서 trajectory(빈 tool_results) 경로가 아닌
    suspicious(숫자목록) 경로를 타게 한다.
    """
    result = reconcile(
        "최근 3 months 비용은 $9999, 요청 1,234건 입니다.",
        _sql_env(count=1234),
    )
    assert result is not None
    assert result["verdict"] == "WARN"
    assert "9999" in result["reason"]
    # phantom 3,000,000 이 suspicious 목록에 없어야
    assert "3000000" not in result["reason"]


def test_real_m_multiplier_still_works(reconcile):
    """'10m 토큰'(공백+토큰) 처럼 m 뒤에 글자 없으면 여전히 million 배수.

    단 '토큰' 은 시간단위 아니므로 10m → 10,000,000 으로 해석되고 근거되면 OK.
    """
    # "$10M" 형태 (뒤에 영문 글자 없음) → 10,000,000 배수 유지
    result = reconcile("총 비용 $10M 입니다.", _sql_env(total=10_000_000))
    assert result is None


# ─── 과잉 억제 방지: 시간단위처럼 보이지만 아닌 단어 ───
@pytest.mark.parametrize(
    "text,desc",
    [
        ("총 비용은 $4567 입니다.", "미근거 통화 숫자 → WARN 유지"),
        ("응답 시간 100초과 사용자가 있습니다.", "'100초과'(초+과=다른 단어) → 100 미근거 WARN"),
        ("처리량 250건이 누락되었습니다.", "미근거 250(건은 시간단위 아님) → WARN"),
    ],
)
def test_ungrounded_nontime_number_still_warns(reconcile, text, desc):
    # tool_results 비어있음 → trajectory WARN 경로
    result = reconcile(text, [])
    assert result is not None, f"{desc} | 미근거 숫자인데 WARN 안 뜸"
    assert result["verdict"] == "WARN"


# ─── 기존 동작 무회귀: K/M 배수, 퍼센트, 연도 ───
def test_km_multiplier_grounded(reconcile):
    # "5M 토큰" → 5,000,000 이 envelope 에 있으면 근거됨
    result = reconcile("총 5M 토큰을 사용했습니다.", _sql_env(tokens=5_000_000))
    assert result is None


def test_percent_excluded(reconcile):
    # 퍼센트는 파생 표시값이라 reconciliation 제외 — 30% 가 근거 없어도 무관
    result = reconcile("점유율은 30% 입니다.", _sql_env(share=0.3))
    assert result is None


def test_year_excluded(reconcile):
    # 1900~2100 연도는 노이즈 필터
    result = reconcile("2026년 기준 사용자는 42명입니다.", _sql_env(users=42))
    assert result is None


def test_small_number_excluded(reconcile):
    # |v|<10 작은 수는 제외
    result = reconcile("상위 5명을 추렸습니다.", [])
    assert result is None


# ─── grounding 핵심 동작 ───
def test_grounded_large_number_passes(reconcile):
    result = reconcile(
        "총 비용은 $4,560 입니다.",
        _sql_env(total=4560),
    )
    assert result is None


def test_ungrounded_with_other_grounded_still_warns(reconcile):
    # 1234 는 근거되지만 9999 는 미근거 → suspicious 경로 WARN
    result = reconcile(
        "요청 1,234건, 비용 $9999 입니다.",
        _sql_env(count=1234),
    )
    assert result is not None
    assert result["verdict"] == "WARN"
    assert "9999" in result["reason"]


def test_time_unit_constant_compiles(agent_pure_fns):
    # _NUM_RE 가 컴파일된 정규식인지 + 시간단위 그룹이 4번인지 sanity
    import re

    num_re = agent_pure_fns["_NUM_RE"]
    assert isinstance(num_re, re.Pattern)
    m = num_re.search("30일")
    assert m is not None
    assert m.group(4) is not None  # 시간단위 그룹 매치
