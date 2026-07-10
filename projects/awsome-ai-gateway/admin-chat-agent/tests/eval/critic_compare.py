# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Critic 비교 실험 하니스 (DEVLOG §59) — GPT vs Claude diverse-lens 검증 효과 측정.

critic_dataset.yaml 의 (question, correct SQL, broken SQL[defect 라벨]) 을 각 critic
(모델별 validator)에 먹여 verdict 를 받고, 다음 지표를 계산한다:

  - detection_rate (recall): broken SQL 을 FAIL/WARN(=잡음)으로 판정한 비율. 높을수록 좋음.
  - false_positive_rate: correct SQL 을 FAIL(=오탐)로 판정한 비율. 낮을수록 좋음.
  - by_defect: 결함 종류별 탐지율(어떤 critic 이 어떤 오류에 강한지).
  - **orthogonality**: critic A 가 놓쳤지만 B 가 잡은 케이스(그 반대도). 멀티모델의
    진짜 가치 — 겹치지 않는 탐지. 둘이 똑같이 잡고 똑같이 놓치면 멀티모델은 비용만 2배.

critic 은 `(question, sql) -> verdict_str("PASS"|"WARN"|"FAIL")` 함수로 추상화.
모델 비결정성 때문에 케이스당 runs 회 돌려 다수결 verdict 를 쓴다.

critic 구현:
  - make_claude_critic(): 배포된 AgentCore 의 ask_validator 경로 또는 로컬 sql_validator
    Agent 호출(GOLDEN_LIVE 환경). Claude(Opus 4.8).
  - make_gpt_critic(): Mantle(GPT) 연결 후 추가(현재 인증 미연결 — placeholder).
    동일 sql_validator 프롬프트, 모델만 GPT 로.

라이브 모델 호출이라 비용 발생. 결과는 JSON 으로 저장해 DEVLOG 에 기록.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Callable, Literal

import yaml

Verdict = Literal["PASS", "WARN", "FAIL"]
Critic = Callable[[str, str], Verdict]

_DATASET = Path(__file__).resolve().parent / "critic_dataset.yaml"


def load_dataset() -> dict:
    with open(_DATASET, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _majority(verdicts: list[str]) -> str:
    """N회 verdict 다수결. 동률이면 더 보수적(FAIL>WARN>PASS) 채택 — 검증은
    의심을 우대(틀린 걸 놓치는 것보다 한 번 더 보는 게 안전)."""
    if not verdicts:
        return "PASS"
    c = Counter(verdicts)
    top = c.most_common()
    max_n = top[0][1]
    tied = [v for v, n in top if n == max_n]
    for pref in ("FAIL", "WARN", "PASS"):
        if pref in tied:
            return pref
    return tied[0]


def _caught(verdict: str) -> bool:
    """critic 이 오류를 '잡았다'고 보는 기준 — FAIL 또는 WARN(둘 다 사용자에게 노출)."""
    return verdict in ("FAIL", "WARN")


def run_critic(critic: Critic, dataset: dict, *, runs: int = 3, label: str = "critic") -> dict:
    """한 critic 으로 데이터셋 전체 평가 → per-item verdict + 집계 지표."""
    items: list[dict] = []  # {kind: correct|broken, id, defect?, expect, verdict, caught}

    for case in dataset.get("cases", []):
        qid = case["id"]
        question = case["question"]

        # correct SQL — PASS 기대(오탐 측정용)
        cv = _majority([critic(question, case["correct"]) for _ in range(runs)])
        items.append({
            "kind": "correct", "id": qid, "defect": None, "expect": "PASS",
            "verdict": cv, "caught": _caught(cv),  # correct 인데 caught=True 면 false positive
        })

        # broken SQL — FAIL/WARN 기대(탐지율 측정용)
        for b in case.get("broken", []):
            bv = _majority([critic(question, b["sql"]) for _ in range(runs)])
            items.append({
                "kind": "broken", "id": qid, "defect": b["defect"], "expect": b["expect"],
                "verdict": bv, "caught": _caught(bv),
            })

    broken = [i for i in items if i["kind"] == "broken"]
    correct = [i for i in items if i["kind"] == "correct"]
    n_detected = sum(1 for i in broken if i["caught"])
    n_fp = sum(1 for i in correct if i["verdict"] == "FAIL")  # correct→FAIL 만 오탐(WARN 은 관대)

    by_defect: dict[str, dict] = {}
    for i in broken:
        d = by_defect.setdefault(i["defect"], {"total": 0, "caught": 0})
        d["total"] += 1
        d["caught"] += 1 if i["caught"] else 0

    return {
        "label": label,
        "runs": runs,
        "detection_rate": round(n_detected / len(broken), 3) if broken else None,
        "detected": n_detected,
        "broken_total": len(broken),
        "false_positive_rate": round(n_fp / len(correct), 3) if correct else None,
        "false_positives": n_fp,
        "correct_total": len(correct),
        "by_defect": {k: {**v, "rate": round(v["caught"] / v["total"], 2)} for k, v in by_defect.items()},
        "items": items,
    }


def compare(result_a: dict, result_b: dict) -> dict:
    """두 critic 결과의 직교성 — 멀티모델의 진짜 가치 측정.

    a_only_caught: A 는 broken 을 잡았지만 B 는 놓침(그 반대 b_only_caught).
    both_caught / both_missed. ensemble(둘 중 하나라도 잡으면 잡음) 탐지율.
    """
    a_items = {(i["id"], i["defect"]): i for i in result_a["items"] if i["kind"] == "broken"}
    b_items = {(i["id"], i["defect"]): i for i in result_b["items"] if i["kind"] == "broken"}
    keys = sorted(set(a_items) | set(b_items))

    a_only, b_only, both, neither = [], [], [], []
    for k in keys:
        ca = a_items.get(k, {}).get("caught", False)
        cb = b_items.get(k, {}).get("caught", False)
        tag = f"{k[0]}/{k[1]}"
        if ca and cb:
            both.append(tag)
        elif ca and not cb:
            a_only.append(tag)
        elif cb and not ca:
            b_only.append(tag)
        else:
            neither.append(tag)

    n = len(keys)
    ensemble_caught = len(both) + len(a_only) + len(b_only)
    return {
        "a_label": result_a["label"],
        "b_label": result_b["label"],
        "both_caught": both,
        "a_only_caught": a_only,   # A 만 잡음 — A 가 B 에 더하는 직교 가치
        "b_only_caught": b_only,   # B 만 잡음 — B 가 A 에 더하는 직교 가치
        "neither_caught": neither,  # 둘 다 놓침 — 멀티모델로도 못 잡는 사각지대
        "ensemble_detection_rate": round(ensemble_caught / n, 3) if n else None,
        "a_detection_rate": result_a["detection_rate"],
        "b_detection_rate": result_b["detection_rate"],
        # 직교성 점수: 한쪽만 잡은 비율 — 높을수록 멀티모델 가치 큼(서로 보완).
        "orthogonality": round((len(a_only) + len(b_only)) / n, 3) if n else None,
        "verdict": (
            "멀티모델 가치 큼(상호보완)" if (len(a_only) + len(b_only)) / max(n, 1) >= 0.2
            else "중복 큼(단일로 충분 가능성)"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Critic 구현 — 모델별. 라이브 호출(GOLDEN_LIVE).
# ─────────────────────────────────────────────────────────────────────────────
def make_claude_critic() -> Critic:
    """Claude(Opus 4.8) validator — 배포 agent 의 sql_validator 프롬프트 + sql_struct
    AST 메타(L2 와 동일 입력)로 verdict. AWS 자격증명 필요."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from agent import main as m  # noqa

    def critic(question: str, sql: str) -> Verdict:
        env = {"sql": sql, "rows": [], "row_count": 0, "columns": []}
        m._auto_validate(question, env)
        v = (env.get("validation") or {}).get("verdict", "PASS")
        return v if v in ("PASS", "WARN", "FAIL") else "PASS"

    return critic


def make_gpt_critic() -> Critic:
    """GPT-5.5(Bedrock Mantle, us-east-2 Responses API) 역번역 critic — AgentCore_only
    검증 경로(BedrockOpenAI + provide_token). main.py._gpt_critic_call 재사용해
    동일 sql_critic 프롬프트로 호출. openai + aws_bedrock_token_generator 필요.

    GPT-5.5 가 불안정(500/빈스트림)하면 verdict 가 PASS 폴백되거나 예외 — 실측 시
    안정성 확인 후 사용(§59). 다른 패밀리(OpenAI)라 diverse-lens 핵심.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from agent import main as m  # noqa

    def critic(question: str, sql: str) -> Verdict:
        from agent import sql_struct
        facts = sql_struct.extract_facts(sql)
        payload = json.dumps({
            "user_question": question, "generated_sql": sql,
            "sample_rows": [], "sql_structure": sql_struct.facts_to_prompt(facts),
        })
        text = m._gpt_critic_call(payload)
        parsed = m._parse_agent_json(text, expect_keys=("verdict", "restated_intent", "reason")) or {}
        v = str(parsed.get("verdict", "PASS")).upper()
        return v if v in ("PASS", "WARN", "FAIL") else "PASS"

    return critic


if __name__ == "__main__":
    # 사용: GOLDEN_LIVE=1 AGENTCORE_RUNTIME_ARN=... python -m tests.eval.critic_compare
    ds = load_dataset()
    runs = int(os.environ.get("CRITIC_RUNS", "3"))
    out_path = os.environ.get("CRITIC_OUT", "/tmp/critic_claude.json")

    claude = make_claude_critic()
    res = run_critic(claude, ds, runs=runs, label="claude-opus-4-8")
    Path(out_path).write_text(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"Claude critic: detection={res['detection_rate']} fp={res['false_positive_rate']}")
    print(f"  by_defect: {json.dumps(res['by_defect'], ensure_ascii=False)}")
    print(f"  saved → {out_path}")
