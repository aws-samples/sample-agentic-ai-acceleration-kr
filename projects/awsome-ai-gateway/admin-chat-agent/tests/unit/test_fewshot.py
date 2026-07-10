# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""DAIL-SQL few-shot 선택/주입 + 오염 가드 테스트.

가장 중요한 불변식: few-shot 뱅크가 golden 평가 질문의 정답을 노출하지 않는다
(train/test contamination 방지). 두 겹으로 검증:
  1. 뱅크 질문 ∩ golden 질문 = ∅ (정규화 기준)
  2. LOO 가드: 평가 질문과 동일 질문은 select 에서 제외
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest
import yaml

from agent import fewshot

_GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"


def _golden_questions() -> list[str]:
    qs = []
    for p in glob.glob(str(_GOLDEN_DIR / "**" / "*.yaml"), recursive=True):
        with open(p, encoding="utf-8") as f:
            case = yaml.safe_load(f)
        if case and case.get("question"):
            qs.append(case["question"])
    return qs


def test_bank_loads():
    bank = fewshot._load_bank()
    assert len(bank) >= 10  # 12개 기대
    for ex in bank:
        assert ex.get("question") and ex.get("sql")


def test_bank_disjoint_from_golden():
    """★ 핵심 오염 가드: 뱅크 질문이 golden 평가 질문과 정규화 일치하면 안 됨."""
    bank_norm = {fewshot._normalize(e["question"]) for e in fewshot._load_bank()}
    golden_norm = {fewshot._normalize(q) for q in _golden_questions()}
    overlap = bank_norm & golden_norm
    assert not overlap, f"few-shot 뱅크가 golden 질문과 겹침(오염): {overlap}"


def test_loo_guard_excludes_identical_question():
    """뱅크에 있는 질문 그대로 select 하면 그 질문 자신은 제외(LOO)."""
    bank = fewshot._load_bank()
    sample_q = bank[0]["question"]
    selected = fewshot.select_examples(sample_q, k=5)
    sel_norm = {fewshot._normalize(e["question"]) for e in selected}
    assert fewshot._normalize(sample_q) not in sel_norm


def test_select_returns_relevant_for_golden_question():
    """golden 평가 질문에 대해 도메인이 맞는 예시를 고른다(빈 결과 아님)."""
    sel = fewshot.select_examples("지금 활성 VK 가장 많은 사용자", k=2)
    assert sel  # 비어있지 않음
    joined = " ".join(e["question"] + e["sql"] for e in sel)
    assert "virtual_keys" in joined or "VK" in joined


def test_select_k_limit():
    sel = fewshot.select_examples("이번 달 비용 top 10 사용자", k=2)
    assert len(sel) <= 2


def test_build_block_nonempty_and_has_sql_fence():
    block = fewshot.build_fewshot_block("팀별 모델 사용 분포", k=2)
    assert block
    assert "```sql" in block
    assert "Few-shot Examples" in block


def test_build_block_empty_when_no_match():
    """전혀 안 겹치는 질문이면 빈 블록(주입 없음 → 기존 동작)."""
    block = fewshot.build_fewshot_block("zzz qqq xyzzy 무관한단어", k=3)
    # 토큰 겹침 0 → 빈 블록 (graceful degrade)
    assert block == ""


@pytest.mark.parametrize("q", _golden_questions())
def test_no_golden_question_leaks_its_answer(q):
    """모든 golden 질문에 대해, 그 질문과 동일한 예시가 주입되지 않는다."""
    selected = fewshot.select_examples(q, k=5)
    sel_norm = {fewshot._normalize(e["question"]) for e in selected}
    assert fewshot._normalize(q) not in sel_norm
