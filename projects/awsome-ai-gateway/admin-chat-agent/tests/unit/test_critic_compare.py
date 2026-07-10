# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""critic_compare 지표 로직 단위 테스트 (DEVLOG §59).

라이브 모델 없이 합성 critic 으로 detection/false-positive/orthogonality 계산을 검증.
이 지표가 GPT 도입 여부를 결정하므로 계산이 정확해야 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.eval import critic_compare as cc  # noqa: E402

# 미니 데이터셋 — correct 1 + broken 2(fanout, timezone).
_DS = {
    "cases": [
        {
            "id": "t1",
            "question": "q1",
            "correct": "SELECT 1",
            "broken": [
                {"defect": "fanout", "expect": "FAIL", "sql": "BROKEN_FANOUT"},
                {"defect": "timezone", "expect": "FAIL", "sql": "BROKEN_TZ"},
            ],
        },
    ]
}


def _critic_from_map(verdict_map: dict[str, str]) -> cc.Critic:
    """sql 텍스트 → verdict 매핑 가짜 critic(결정적)."""
    return lambda _q, sql: verdict_map.get(sql, "PASS")  # noqa: E731


def test_majority_tie_prefers_conservative():
    assert cc._majority(["PASS", "FAIL"]) == "FAIL"      # 동률 → 보수적
    assert cc._majority(["PASS", "PASS", "FAIL"]) == "PASS"
    assert cc._majority(["WARN", "FAIL", "PASS"]) == "FAIL"


def test_perfect_critic_metrics():
    # correct→PASS, 둘 다 잡음 → detection 1.0, fp 0.0
    critic = _critic_from_map({"SELECT 1": "PASS", "BROKEN_FANOUT": "FAIL", "BROKEN_TZ": "WARN"})
    r = cc.run_critic(critic, _DS, runs=1, label="perfect")
    assert r["detection_rate"] == 1.0
    assert r["false_positive_rate"] == 0.0
    assert r["by_defect"]["fanout"]["rate"] == 1.0
    assert r["by_defect"]["timezone"]["rate"] == 1.0


def test_false_positive_counted():
    # correct 를 FAIL 로 → 오탐. broken 은 다 놓침.
    critic = _critic_from_map({"SELECT 1": "FAIL", "BROKEN_FANOUT": "PASS", "BROKEN_TZ": "PASS"})
    r = cc.run_critic(critic, _DS, runs=1, label="bad")
    assert r["detection_rate"] == 0.0
    assert r["false_positive_rate"] == 1.0


def test_orthogonality_complementary():
    # A 는 fanout 만, B 는 timezone 만 잡음 → 상호보완(직교성 높음).
    a = _critic_from_map({"SELECT 1": "PASS", "BROKEN_FANOUT": "FAIL", "BROKEN_TZ": "PASS"})
    b = _critic_from_map({"SELECT 1": "PASS", "BROKEN_FANOUT": "PASS", "BROKEN_TZ": "FAIL"})
    ra = cc.run_critic(a, _DS, runs=1, label="A")
    rb = cc.run_critic(b, _DS, runs=1, label="B")
    comp = cc.compare(ra, rb)
    assert comp["a_only_caught"] == ["t1/fanout"]
    assert comp["b_only_caught"] == ["t1/timezone"]
    assert comp["both_caught"] == []
    assert comp["ensemble_detection_rate"] == 1.0  # 둘이 합치면 다 잡음
    assert comp["orthogonality"] == 1.0
    assert "멀티모델 가치 큼" in comp["verdict"]


def test_orthogonality_redundant():
    # A·B 가 똑같이 fanout 만 잡고 timezone 둘 다 놓침 → 중복(멀티모델 가치 작음).
    same = _critic_from_map({"SELECT 1": "PASS", "BROKEN_FANOUT": "FAIL", "BROKEN_TZ": "PASS"})
    ra = cc.run_critic(same, _DS, runs=1, label="A")
    rb = cc.run_critic(same, _DS, runs=1, label="B")
    comp = cc.compare(ra, rb)
    assert comp["both_caught"] == ["t1/fanout"]
    assert comp["neither_caught"] == ["t1/timezone"]
    assert comp["orthogonality"] == 0.0
    assert "중복 큼" in comp["verdict"]


def test_real_dataset_loads():
    ds = cc.load_dataset()
    assert len(ds["cases"]) >= 5
    # 각 케이스에 correct + broken(defect 라벨) 존재
    for c in ds["cases"]:
        assert c["correct"].strip()
        for b in c["broken"]:
            assert b["defect"] and b["expect"] in ("FAIL", "WARN") and b["sql"].strip()
