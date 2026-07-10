# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Golden test runner — 12 use case 정확도 측정 CLI.

docs/admin-chat-agent-spec.md §8.5.2 / §8.5.3.

사용법:
  # static 만 (라이브 호출 없음, 비용 0). 골든 케이스 자체 정합성 검증.
  python -m tests.eval.run_golden --static

  # 라이브 E2E (배포된 dev runtime 호출 → 정확도 PASS율 정량화).
  GOLDEN_LIVE=1 \
  AGENTCORE_RUNTIME_ARN=arn:aws:bedrock-agentcore:ap-northeast-2:123456789012:runtime/llm_gateway_dev_admin_chat_agent-a8AdBh8WM8 \
  python -m tests.eval.run_golden --live

  # 특정 케이스만
  python -m tests.eval.run_golden --live --case 09

timeout: tier 별 자동 — Tier B(Code Interpreter, 느림) 600s / Tier A 180s.
  GOLDEN_TIMEOUT 환경변수를 주면 모든 케이스에 그 값으로 override.

종료 코드: pass_rate < --min-pass-rate (default 0.0 for static, 0.9 for live) 면 1.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

# tests/ 를 import path 에 (python -m 또는 직접 실행 모두 대응)
_HERE = Path(__file__).resolve()
_TESTS_DIR = _HERE.parent.parent
if str(_TESTS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR.parent))

from tests.eval import scoring  # noqa: E402

GOLDEN_DIR = _TESTS_DIR / "golden"


def load_cases(case_filter: str | None = None, tier_filter: str | None = None) -> list[dict]:
    """tests/golden/**/*.yaml 로드. case_filter 면 use_case_id 일치만,
    tier_filter('A'|'B') 면 해당 tier 만. (A/B 모델 비교 시 Tier A=SQL-only 만
    돌려 SQL 신호를 분리하고 느린 Tier B 를 건너뛰는 용도.)"""
    import yaml

    cases = []
    for path in sorted(glob.glob(str(GOLDEN_DIR / "**" / "*.yaml"), recursive=True)):
        with open(path, encoding="utf-8") as f:
            case = yaml.safe_load(f)
        case["_path"] = path
        if case_filter and str(case.get("use_case_id")) != str(case_filter):
            continue
        if tier_filter and str(case.get("tier", "")).upper() != tier_filter.upper():
            continue
        cases.append(case)
    return cases


def run_static(cases: list[dict]) -> list[dict]:
    """라이브 호출 없이 골든 케이스 자체의 정합성만 검증.

    expected.sql.required_clauses 와 forbidden_clauses 가 서로 모순되지
    않는지, required/forbidden 패턴이 valid regex 인지, agent_path 가
    유효한 tool 이름인지 등 — 케이스 정의가 깨지지 않았음을 보장한다.
    (실제 정확도가 아니라 '테스트 자산의 무결성' 측정.)
    """
    import re as _re

    VALID_TOOLS = {
        "ask_sql_specialist",
        "ask_code_specialist",
        "ask_validator",
        "ask_viz_specialist",
        "render_chart",
    }
    results = []
    for case in cases:
        checks: dict[str, bool] = {}
        details: dict = {}
        exp = case.get("expected", {})

        checks["has_question"] = bool(case.get("question"))
        checks["has_use_case_id"] = bool(case.get("use_case_id"))
        checks["valid_tier"] = case.get("tier") in ("A", "B")

        # 정규식 컴파일 가능?
        all_patterns = []
        sql_exp = exp.get("sql", {})
        for key in ("required_clauses", "forbidden_clauses"):
            all_patterns += sql_exp.get(key, [])
        bad = []
        for p in all_patterns:
            try:
                _re.compile(p)
            except _re.error:
                bad.append(p)
        checks["valid_regex_patterns"] = not bad
        if bad:
            details["invalid_regex"] = bad

        # forbidden 과 required 가 동일 패턴으로 충돌하지 않는지
        req = set(sql_exp.get("required_clauses", []))
        forb = set(sql_exp.get("forbidden_clauses", []))
        checks["no_required_forbidden_conflict"] = not (req & forb)

        # agent_path 항목이 실제 tool 이름인지
        path_tools = set(exp.get("agent_path_includes", [])) | set(
            exp.get("agent_path_excludes", [])
        )
        unknown = path_tools - VALID_TOOLS
        checks["valid_agent_path_tools"] = not unknown
        if unknown:
            details["unknown_tools"] = sorted(unknown)

        # Tier B 는 code expected 필수
        if case.get("tier") == "B":
            checks["tier_b_has_code"] = bool(exp.get("code"))
            checks["tier_b_forces_code_specialist"] = (
                "ask_code_specialist" in exp.get("agent_path_includes", [])
            )

        passed = all(checks.values())
        results.append(
            {
                "case_id": case.get("use_case_id"),
                "tier": case.get("tier"),
                "name": case.get("name"),
                "checks": checks,
                "details": details,
                "passed": passed,
                "pass_rate": round(sum(checks.values()) / len(checks), 3),
                "error": None,
            }
        )
    return results


def _score_one(case, agent_client, run_token, case_timeout, run_idx) -> dict:
    """케이스 1회 invoke + 채점. 예외는 FAIL 결과로 흡수."""
    cid = case.get("use_case_id")
    try:
        events = agent_client.invoke_agent(
            case["question"],
            # run_idx 까지 세션ID 에 넣어 같은 케이스의 N회 실행이 충돌/오염 없도록.
            session_id=f"golden-eval-{cid}-{run_token}-r{run_idx}-{'0' * 10}",
            timeout=case_timeout,
            # deep 모드 케이스(§57) — yaml 에 mode: deep 이면 orchestrator_deep 경로.
            mode=case.get("mode", "quick"),
        )
        result = scoring.score_case(case, events)
        result["_event_count"] = len(events)
        return result
    except Exception as exc:  # noqa: BLE001 — 한 실행 실패가 전체를 막지 않게
        return {
            "case_id": cid,
            "tier": case.get("tier"),
            "name": case.get("name"),
            "checks": {},
            "details": {"exception": f"{type(exc).__name__}: {exc}"},
            "passed": False,
            "pass_rate": 0.0,
            "error": str(exc),
        }


def run_live(cases: list[dict], runs: int = 1) -> list[dict]:
    """배포된 runtime 을 케이스마다 호출 → 이벤트 스트림 채점.

    runs>1 이면 케이스마다 N회 실행해 scoring.reduce_runs 로 다수결 집계 →
    LLM 비결정성으로 인한 단일 실행 변동을 분리(⑤).
    """
    from tests.eval import agent_client

    if not agent_client.is_live_enabled():
        print(
            "ERROR: live 모드엔 GOLDEN_LIVE=1 + AGENTCORE_RUNTIME_ARN 필요.",
            file=sys.stderr,
        )
        sys.exit(2)

    # 실행별 유니크 토큰 — 같은 케이스 재실행 시 세션 충돌("Agent is already
    # processing")과 대화 history 누적(평가 오염)을 막는다. pid 로 충분히 유니크.
    run_token = f"{os.getpid():08d}"

    # tier 별 기본 timeout — Tier B(Code Interpreter sandbox)는 느려 길게,
    # Tier A(SQL only)는 짧게. GOLDEN_TIMEOUT env 가 있으면 명시 override.
    env_timeout = os.environ.get("GOLDEN_TIMEOUT")

    results = []
    for i, case in enumerate(cases):
        cid = case.get("use_case_id")
        print(f"  [{i + 1}/{len(cases)}] case {cid}: {case.get('name')} "
              f"(×{runs}) …", flush=True)
        if env_timeout:
            case_timeout = float(env_timeout)
        else:
            case_timeout = 600.0 if case.get("tier") == "B" else 180.0

        if runs <= 1:
            result = _score_one(case, agent_client, run_token, case_timeout, 0)
            status = "PASS" if result["passed"] else "FAIL"
            print(f"        → {status} (check {result['pass_rate']:.0%})", flush=True)
        else:
            case_runs = []
            for r_idx in range(runs):
                one = _score_one(case, agent_client, run_token, case_timeout, r_idx)
                case_runs.append(one)
                print(f"        run {r_idx + 1}/{runs}: "
                      f"{'PASS' if one['passed'] else 'FAIL'} "
                      f"({one['pass_rate']:.0%})", flush=True)
            result = scoring.reduce_runs(case_runs)
            m = result.get("majority", {})
            mark = "PASS" if result["passed"] else "FAIL"
            print(f"        → {mark} majority {m.get('pass_count')}/{m.get('runs')} "
                  f"(check avg {result['pass_rate']:.0%}, "
                  f"{'stable' if m.get('stable') else 'FLAKY'})", flush=True)
        results.append(result)
    return results


def print_report(results: list[dict], summary: dict, mode: str) -> None:
    print()
    print(f"═══ Golden Test Report ({mode}) ═══")
    for r in results:
        mark = "✅" if r["passed"] else "❌"
        failed = [k for k, v in r["checks"].items() if not v]
        extra = f"  실패체크: {failed}" if failed else ""
        maj = r.get("majority")
        maj_str = ""
        if maj:
            flake = "" if maj.get("stable") else " ⚡FLAKY"
            maj_str = f"  [{maj['pass_count']}/{maj['runs']}{flake}]"
        print(f"  {mark} [{r['tier']}] {r['case_id']} {r['name']} "
              f"— {r['pass_rate']:.0%}{maj_str}{extra}")
        if r.get("error"):
            print(f"       error: {r['error']}")
    print()
    print(f"  Total: {summary['total']}  Passed: {summary['passed']}  "
          f"Pass rate: {summary['pass_rate']:.1%}  "
          f"(check-level {summary.get('mean_check_pass_rate', 0):.1%})")
    for tier, b in sorted(summary.get("by_tier", {}).items()):
        print(f"    Tier {tier}: {b['passed']}/{b['total']} ({b['pass_rate']:.0%})")


def main() -> int:
    ap = argparse.ArgumentParser(description="admin-chat-agent golden test runner")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--static", action="store_true", help="케이스 정합성만 (비용 0)")
    g.add_argument("--live", action="store_true", help="배포 runtime 호출 (비용 발생)")
    ap.add_argument("--case", help="특정 use_case_id 만")
    ap.add_argument("--tier", choices=["A", "B", "a", "b"],
                    help="해당 tier 만 (A=SQL-only, B=SQL+Code). A/B 모델 비교 시 "
                         "Tier A 만 돌려 SQL 신호 분리 + 느린 Tier B 건너뛰기")
    ap.add_argument("--runs", type=int, default=1,
                    help="케이스당 반복 횟수(live). >1 이면 다수결 집계로 변동성 분리")
    ap.add_argument("--min-pass-rate", type=float, default=None)
    ap.add_argument("--json", dest="json_out", help="결과 JSON 저장 경로")
    args = ap.parse_args()

    mode = "live" if args.live else "static"
    cases = load_cases(args.case, args.tier)
    if not cases:
        print("로드된 골든 케이스 없음.", file=sys.stderr)
        return 2

    results = run_live(cases, runs=args.runs) if args.live else run_static(cases)
    summary = scoring.aggregate(results)
    print_report(results, summary, mode)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n  결과 저장: {args.json_out}")

    threshold = args.min_pass_rate
    if threshold is None:
        threshold = 0.9 if args.live else 1.0
    return 0 if summary["pass_rate"] >= threshold else 1


if __name__ == "__main__":
    sys.exit(main())
