# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""골든 케이스 자산의 무결성 검증 (라이브 호출 없음).

12개 use case YAML 이 잘 정의됐는지 — 유효한 정규식, tier/필드 일관성,
실제 tool 이름 사용 — 를 CI 에서 무료로 검증한다. 케이스 정의가 깨지면
라이브 평가 자체가 무의미하므로 이 게이트가 선행한다.
"""

from __future__ import annotations

import pytest

from tests.eval import run_golden


@pytest.fixture(scope="module")
def cases():
    loaded = run_golden.load_cases()
    assert loaded, "tests/golden 에서 케이스를 못 읽음"
    return loaded


def test_exactly_14_cases(cases):
    # 12 기본 + 13(deep plan-first, §57) + 14(auditor crying-wolf 가드, §60)
    assert len(cases) == 14, f"14개 use case 기대, {len(cases)}개 로드됨"


def test_use_case_ids_complete(cases):
    ids = sorted(int(c["use_case_id"]) for c in cases)
    assert ids == list(range(1, 15)), f"use_case_id 1..14 기대, got {ids}"


def test_tier_split(cases):
    tier_a = [c for c in cases if c["tier"] == "A"]
    tier_b = [c for c in cases if c["tier"] == "B"]
    # Tier A 10개 = SQL-only 8 + deep plan-first 1(case 13) + auditor 가드 1(case 14, §60)
    assert len(tier_a) == 10, "Tier A 10개 기대"
    assert len(tier_b) == 4, "Tier B 4개 기대"


def test_all_cases_static_valid(cases):
    """run_golden 의 static 검증 — 모든 케이스가 무결성 체크 통과."""
    results = run_golden.run_static(cases)
    failed = [r for r in results if not r["passed"]]
    assert not failed, "정합성 실패 케이스: " + "; ".join(
        f"{r['case_id']}({[k for k,v in r['checks'].items() if not v]} {r['details']})"
        for r in failed
    )


def test_ground_truth_schema_tables_only(cases):
    """required_tables 가 ground-truth 스키마(usage/budget/model/auth)만 쓰는지.

    config/golden_examples.yaml 의 옛 public.* 네임스페이스 drift 가 골든
    케이스로 새지 않도록 가드 (memory/chat-agent-schema-drift).
    """
    BAD_PREFIXES = ("public.usage_logs", "public.budgets", "public.rate_limits")
    for c in cases:
        tables = c.get("expected", {}).get("sql", {}).get("required_tables", [])
        for t in tables:
            assert not t.startswith("public."), (
                f"case {c['use_case_id']}: '{t}' 는 옛 public.* 스키마. "
                f"ground truth 는 usage/budget/model 스키마 (schema drift 가드)."
            )
            assert t not in BAD_PREFIXES


def test_tier_b_forces_code_specialist(cases):
    for c in cases:
        if c["tier"] == "B":
            includes = c["expected"].get("agent_path_includes", [])
            assert "ask_code_specialist" in includes, (
                f"Tier B case {c['use_case_id']} 는 ask_code_specialist 강제 필요"
            )
            assert c["expected"].get("code"), (
                f"Tier B case {c['use_case_id']} 는 code expected 필요"
            )
