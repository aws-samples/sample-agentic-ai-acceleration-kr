# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Golden test scoring — pure functions over the agent's SSE event stream.

docs/admin-chat-agent-spec.md §8.5.2. 의 evaluation harness 를 *실제 이벤트
계약* 에 맞춰 구현한다. spec 의 의사코드는 `response.tool_calls.query_db.sql`
처럼 단순화돼 있지만, 실제는 agents-as-tools 구조라 SQL/code/verdict 가
sub-agent 의 구조화 envelope(tool_result.result.*)에 들어 있다.

이벤트 추출 경로 (memory/chat-agent-event-contract):
  - 생성 SQL  : tool_result(tool==ask_sql_specialist).result.sql
  - 실행 코드 : tool_result(tool==ask_code_specialist).result.code
  - validator : tool_result(tool==ask_validator).result.verdict
  - chart kind: chart.spec.kind
  - agent path: tool_call 이벤트의 tool 이름 순서

이 모듈은 boto3/strands 의존성이 없어 라이브 호출 없이 단위 테스트 가능하다.
실제 invoke 는 tests/eval/agent_client.py 가 담당.
"""

from __future__ import annotations

import json
import re
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Event-stream extractors — 검증된 이벤트 계약에 따라 필드를 끌어낸다.
# ─────────────────────────────────────────────────────────────────────────────
# L3(§58): ask_sql_verified 는 ask_sql_specialist 의 실행검증 변형(k후보 실행→다수결).
# 경로 검사·휴리스틱에선 동일 'SQL 생성' 단계로 취급해야 한다(둘 다 SQL Specialist 호출).
_PATH_ALIASES = {"ask_sql_verified": "ask_sql_specialist"}


def extract_agent_path(events: list[dict]) -> list[str]:
    """orchestrator 가 거친 specialist 경로.

    tool_call 이벤트(orchestrator 가 직접 부른 도구) **+** tool_result 이벤트
    (내부 자동 단계도 포함)를 합쳐 본다. §58 부터 validator 는 SQL 도구 안에서
    **자동 실행**되므로 tool_call 이벤트가 없고 tool_result 로만 나타난다 — 경로
    검사가 tool_call 만 보면 '검증 안 함'으로 오판한다. 두 소스를 합쳐 등장 순서
    유지(중복 제거). ask_sql_verified → ask_sql_specialist 정규화.
    """
    path: list[str] = []
    for e in events:
        if not isinstance(e, dict) or not e.get("tool"):
            continue
        if e.get("type") in ("tool_call", "tool_result"):
            name = _PATH_ALIASES.get(e["tool"], e["tool"])
            if name not in path:
                path.append(name)
    return path


def _tool_results(events: list[dict], tool: str) -> list[dict]:
    """tool_result 이벤트의 result 중 dict 인 것만 (malformed 프레임 방어).

    스트림은 json.loads 산물이라 result 가 list/str 일 수 있다 — 그대로
    .get() 하면 AttributeError. dict 가 아닌 result 는 건너뛴다.
    """
    out: list[dict] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "tool_result" and e.get("tool") == tool:
            r = e.get("result")
            if isinstance(r, dict):
                out.append(r)
    return out


def extract_sql(events: list[dict]) -> str:
    """ask_sql_specialist envelope 들의 sql 을 연결 (다중 호출 대비)."""
    sqls = [
        str(r.get("sql", ""))
        for r in _tool_results(events, "ask_sql_specialist")
        if r.get("sql")
    ]
    return "\n".join(sqls)


def extract_code(events: list[dict]) -> str:
    """ask_code_specialist envelope 들의 code 를 연결."""
    codes = [
        str(r.get("code", ""))
        for r in _tool_results(events, "ask_code_specialist")
        if r.get("code")
    ]
    return "\n".join(codes)


def extract_code_summary(events: list[dict]) -> str:
    """ask_code_specialist envelope 들의 result_summary 를 연결.

    Code Specialist 가 execute_python 을 실제로 돌리고도 `code` 필드를 비우는
    경우가 있어(DEVLOG §32), 기법 검증의 fallback 근거로 result_summary 를
    함께 본다. summary 엔 "SARIMAX(1,1,1)..." 처럼 실제 실행한 기법이 적힌다.
    """
    summaries = [
        str(r.get("result_summary", ""))
        for r in _tool_results(events, "ask_code_specialist")
        if r.get("result_summary")
    ]
    return "\n".join(summaries)


def code_specialist_called(events: list[dict]) -> bool:
    """ask_code_specialist 가 호출됐고 envelope 를 반환했는지."""
    return bool(_tool_results(events, "ask_code_specialist"))


def extract_validator_verdict(events: list[dict]) -> str | None:
    """ask_validator 의 최종 verdict (마지막 호출 기준)."""
    verdicts = [
        r.get("verdict")
        for r in _tool_results(events, "ask_validator")
        if r.get("verdict")
    ]
    return verdicts[-1] if verdicts else None


def extract_validator_confidence(events: list[dict]) -> float | None:
    confs = [
        r.get("confidence")
        for r in _tool_results(events, "ask_validator")
        if isinstance(r.get("confidence"), (int, float))
    ]
    return float(confs[-1]) if confs else None


def extract_chart_kinds(events: list[dict]) -> list[str]:
    """chart 이벤트들의 spec.kind (spec 이 dict 일 때만)."""
    kinds = []
    for e in events:
        if isinstance(e, dict) and e.get("type") == "chart":
            spec = e.get("spec")
            if isinstance(spec, dict) and spec.get("kind"):
                kinds.append(spec["kind"])
    return kinds


def extract_full_text(events: list[dict]) -> str:
    """text 델타 누적 = 최종 답변 본문."""
    return "".join(
        e.get("chunk", "")
        for e in events
        if isinstance(e, dict) and e.get("type") == "text"
    )


def has_error(events: list[dict]) -> str | None:
    """in-band 에러 프레임 탐지."""
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "error" or e.get("error_type"):
            return e.get("error") or e.get("error_type") or "stream error"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Check helpers — expected spec 의 각 항목을 채점.
# ─────────────────────────────────────────────────────────────────────────────
def _norm_sql(sql: str) -> str:
    """공백 정규화만 (clause 매칭용). 대소문자는 re.IGNORECASE 로 처리 —
    패턴을 lower() 하면 \\D/\\W/\\S 같은 메타문자 의미가 뒤집히므로 금지.
    """
    return re.sub(r"\s+", " ", sql)


def check_required_tables(sql: str, required: list[str]) -> tuple[bool, list[str]]:
    """required_tables 가 SQL 에 모두 등장하는지. schema-qualified 또는
    table 단독 둘 다 허용(에이전트가 search_path 로 schema 생략 가능).

    반환: (통과여부, 누락목록)
    """
    norm = _norm_sql(sql)
    missing = []
    for t in required:
        bare = t.split(".")[-1]
        # "schema.table" 정확 매치(대소문자 무시) 또는 "table" 단독 매치(워드 경계)
        if re.search(re.escape(t), norm, re.IGNORECASE):
            continue
        if re.search(
            r"(^|[^.\w])" + re.escape(bare) + r"($|[^.\w])", norm, re.IGNORECASE
        ):
            continue
        missing.append(t)
    return (not missing, missing)


def check_required_clauses(sql: str, patterns: list[str]) -> tuple[bool, list[str]]:
    """각 패턴(정규식, 대소문자 무시)이 SQL 에 매치되는지.

    패턴은 그대로 두고 re.IGNORECASE 적용 (메타문자 보존).
    """
    norm = _norm_sql(sql)
    missing = [p for p in patterns if not re.search(p, norm, re.IGNORECASE)]
    return (not missing, missing)


def check_forbidden_clauses(sql: str, patterns: list[str]) -> tuple[bool, list[str]]:
    """금지 패턴이 하나도 없어야 통과. 매치되면 위반 목록 반환."""
    norm = _norm_sql(sql)
    hits = [p for p in patterns if re.search(p, norm, re.IGNORECASE)]
    return (not hits, hits)


def check_timezone_anchored(sql: str) -> tuple[bool, str]:
    """시간 필터가 있으면 KST(Asia/Seoul) 앵커가 강제됐는지 결정적 검사 (§58).

    감사 결함 ③/⑤: 기존 required_clauses 정규식은 'interval'/'now()' 존재만 봐서
    KST 미앵커 UTC SQL(`requested_at >= now() - interval '7 days'`)도 통과했다 —
    9시간 경계 오차가 회귀로 안 잡힘. 이 체크는 시간 경계 표현이 있는데
    `AT TIME ZONE 'Asia/Seoul'` 가 없으면 FAIL.

    반환: (통과, 사유). 시간 필터가 아예 없으면 통과(해당없음).
    """
    norm = _norm_sql(sql)
    # 시간 경계 신호: now()/interval/date_trunc 가 등장하는가
    has_time = bool(
        re.search(r"\bnow\s*\(\s*\)", norm, re.IGNORECASE)
        or re.search(r"\binterval\b", norm, re.IGNORECASE)
        or re.search(r"\bdate_trunc\b", norm, re.IGNORECASE)
    )
    if not has_time:
        return (True, "시간 필터 없음(해당없음)")
    anchored = bool(re.search(r"at\s+time\s+zone\s+'asia/seoul'", norm, re.IGNORECASE))
    if anchored:
        return (True, "KST 앵커 확인")
    return (
        False,
        "시간 경계(now/interval/date_trunc)가 KST(AT TIME ZONE 'Asia/Seoul') 앵커 "
        "없이 사용됨 — UTC 기준이면 KST 자정과 9시간 어긋남(결함③).",
    )


def check_code_imports(code: str, required: list[str]) -> tuple[bool, list[str]]:
    """required_imports 각 패키지가 import 됐는지 (import X / from X)."""
    missing = []
    for pkg in required:
        if not re.search(rf"\b(?:import|from)\s+{re.escape(pkg)}", code):
            missing.append(pkg)
    return (not missing, missing)


def check_code_any_of(code: str, options: list[str]) -> tuple[bool, list[str]]:
    """options 중 하나라도 코드에 등장하면 통과."""
    if not options:
        return (True, [])
    hit = any(re.search(re.escape(o), code, re.IGNORECASE) for o in options)
    return (hit, [] if hit else options)


def check_agent_path_includes(path: list[str], required: list[str]) -> tuple[bool, list[str]]:
    missing = [a for a in required if a not in path]
    return (not missing, missing)


def check_agent_path_excludes(path: list[str], excluded: list[str]) -> tuple[bool, list[str]]:
    hits = [a for a in excluded if a in path]
    return (not hits, hits)


def check_chart_kind(kinds: list[str], allowed: list[str]) -> bool:
    """관측된 chart kind 중 하나라도 allowed 에 속하면 통과.
    차트가 없으면(빈 리스트) — table 같은 게 allowed 면 관대하게 통과.
    """
    if not kinds:
        # 차트 미발행: kpi/table 처럼 차트 없이도 되는 케이스 허용
        return any(k in ("table", "kpi") for k in allowed)
    return any(k in allowed for k in kinds)


# ─────────────────────────────────────────────────────────────────────────────
# Case scorer — 한 케이스의 expected 전체를 events 에 대해 채점.
# ─────────────────────────────────────────────────────────────────────────────
def score_case(case: dict, events: list[dict]) -> dict:
    """golden case(YAML 로드) + 관측 events → 채점 결과 dict.

    반환:
      {
        case_id, tier, name,
        checks: {check_name: bool, ...},
        details: {check_name: 부가정보},
        passed: bool,        # 모든 check 통과
        pass_rate: float,    # 통과 check 비율
        error: str | None,   # stream 에러 시
      }
    """
    expected = case.get("expected", {})
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}

    err = has_error(events)
    if err:
        return {
            "case_id": case.get("use_case_id"),
            "tier": case.get("tier"),
            "name": case.get("name"),
            "checks": {},
            "details": {"stream_error": err},
            "passed": False,
            "pass_rate": 0.0,
            "error": err,
        }

    sql = extract_sql(events)
    code = extract_code(events)
    code_summary = extract_code_summary(events)
    code_called = code_specialist_called(events)
    path = extract_agent_path(events)
    chart_kinds = extract_chart_kinds(events)

    # 1) SQL 검증
    sql_exp = expected.get("sql")
    if sql_exp:
        if not sql:
            checks["sql_present"] = False
            details["sql_present"] = "ask_sql_specialist 결과에 sql 없음"
        else:
            checks["sql_present"] = True
            if sql_exp.get("required_tables"):
                ok, miss = check_required_tables(sql, sql_exp["required_tables"])
                checks["required_tables"] = ok
                if miss:
                    details["required_tables"] = {"missing": miss}
            if sql_exp.get("required_clauses"):
                ok, miss = check_required_clauses(sql, sql_exp["required_clauses"])
                checks["required_clauses"] = ok
                if miss:
                    details["required_clauses"] = {"missing": miss}
            if sql_exp.get("forbidden_clauses"):
                ok, hits = check_forbidden_clauses(sql, sql_exp["forbidden_clauses"])
                checks["forbidden_clauses"] = ok
                if hits:
                    details["forbidden_clauses"] = {"violations": hits}
            # KST 앵커 검증(§58) — timezone_kst: true 면 시간 필터에 Asia/Seoul 강제.
            # 기존 정규식이 못 잡던 UTC 드리프트(결함③)를 회귀로 차단.
            if sql_exp.get("timezone_kst"):
                ok, reason = check_timezone_anchored(sql)
                checks["timezone_kst"] = ok
                if not ok:
                    details["timezone_kst"] = reason

    # 2) Validator verdict
    val_exp = expected.get("validator")
    if val_exp:
        verdict = extract_validator_verdict(events)
        want = val_exp.get("verdict")
        # validator 기대가 있으면 verdict 가 실제로 관측돼야 한다 — None(미실행
        # 또는 파싱실패)은 불통과. (예전엔 None 을 관대 통과시켜 validator 를
        # 통째로 건너뛴 케이스가 silently pass 했음.)
        if verdict is None:
            checks["validator_verdict"] = False
        elif want == "PASS":
            # PASS 기대에 WARN 은 관대(WARN 도 유효 답변) — FAIL 만 불통과.
            checks["validator_verdict"] = verdict in ("PASS", "WARN")
        else:
            checks["validator_verdict"] = verdict == want
        details["validator_verdict"] = {"observed": verdict, "expected": want}
        if val_exp.get("min_confidence") is not None:
            conf = extract_validator_confidence(events)
            # confidence 미보고면 통과(관대), 보고 시 임계 충족 요구
            checks["validator_confidence"] = (
                conf is None or conf >= val_exp["min_confidence"]
            )
            details["validator_confidence"] = {
                "observed": conf,
                "min": val_exp["min_confidence"],
            }

    # 3) Code (Tier B)
    # Code Specialist 가 execute_python 을 돌리고도 `code` envelope 를 비우는
    # 경우가 있어(DEVLOG §32), code 부재 시 result_summary 를 기법 검증의
    # fallback 근거로 쓴다. 본질은 "Code Specialist 가 실제 분석했는가".
    code_exp = expected.get("code")
    if code_exp:
        # code_present: code 텍스트 또는 (호출+summary) 면 통과
        has_evidence = bool(code) or (code_called and bool(code_summary))
        checks["code_present"] = has_evidence
        if not has_evidence:
            details["code_present"] = "ask_code_specialist 미호출/결과 없음"
        # 기법/import 검증 — code 우선, 없으면 result_summary fallback
        haystack = code if code else code_summary
        if not code and code_summary:
            details["code_fallback"] = "code envelope 비어 result_summary 로 검증"
        if has_evidence and code_exp.get("required_imports"):
            # import 는 code 에만 나타남 — code 없으면 fallback 불가, 관대 통과
            if code:
                ok, miss = check_code_imports(code, code_exp["required_imports"])
                checks["code_imports"] = ok
                if miss:
                    details["code_imports"] = {"missing": miss}
            else:
                checks["code_imports"] = True  # code 부재 — import 검증 불가, summary 로 대체
                details["code_imports"] = "code 부재 — summary fallback (import 미검증)"
        if has_evidence and code_exp.get("required_any_of"):
            ok, opts = check_code_any_of(haystack, code_exp["required_any_of"])
            checks["code_technique"] = ok
            if not ok:
                details["code_technique"] = {
                    "expected_any_of": opts,
                    "checked": "code" if code else "result_summary",
                }

    # 4) Agent path
    if expected.get("agent_path_includes"):
        ok, miss = check_agent_path_includes(path, expected["agent_path_includes"])
        checks["agent_path_includes"] = ok
        details["agent_path"] = path
        if miss:
            details["agent_path_includes"] = {"missing": miss}
    if expected.get("agent_path_excludes"):
        ok, hits = check_agent_path_excludes(path, expected["agent_path_excludes"])
        checks["agent_path_excludes"] = ok
        if hits:
            details["agent_path_excludes"] = {"unexpected": hits}

    # 5) Chart kind
    chart_exp = expected.get("chart")
    if chart_exp and chart_exp.get("kind"):
        checks["chart_kind"] = check_chart_kind(chart_kinds, chart_exp["kind"])
        details["chart_kind"] = {"observed": chart_kinds, "allowed": chart_exp["kind"]}

    # 6) Plan (deep 모드 plan-first, §57) — plan 이벤트 또는 본문 ```plan 펜스.
    plan_exp = expected.get("plan")
    if plan_exp and plan_exp.get("required"):
        plan_obj = None
        for ev in events:
            if ev.get("type") == "plan" and isinstance(ev.get("plan"), dict):
                plan_obj = ev["plan"]
                break
        if plan_obj is None:
            # fallback: 본문 텍스트의 ```plan 펜스
            full_text = "".join(
                ev.get("chunk", "") for ev in events if ev.get("type") == "text"
            )
            m = re.search(r"```plan\s*\n(.*?)\n```", full_text, re.DOTALL)
            if m:
                try:
                    plan_obj = json.loads(m.group(1).strip())
                except (ValueError, TypeError):
                    plan_obj = None
        min_steps = int(plan_exp.get("min_steps", 1))
        steps = (plan_obj or {}).get("steps") or []
        checks["plan_present"] = bool(plan_obj) and len(steps) >= min_steps
        details["plan"] = {
            "found": bool(plan_obj),
            "steps": len(steps),
            "min_steps": min_steps,
        }

    # 7) Audit (L5 독립 답변 감사, §60) — audit 이벤트의 verdict 가 기대와 일치하는지.
    #    PASS 기대면 audit 이벤트 미발행(또는 PASS)이어야 하고, RETRY/NEEDS_REVIEW
    #    기대면 그 verdict 의 audit 이벤트가 있어야 한다.
    audit_exp = expected.get("audit")
    if audit_exp and audit_exp.get("verdict"):
        want = str(audit_exp["verdict"]).upper()
        observed = "PASS"  # 미발행 = 결함 없음 = PASS 로 간주
        for ev in events:
            if ev.get("type") == "audit":
                res = ev.get("result") or {}
                observed = str(res.get("verdict", "")).upper() or observed
                break
        checks["audit_verdict"] = observed == want
        details["audit_verdict"] = {"observed": observed, "expected": want}

    passed = all(checks.values()) if checks else False
    pass_rate = (sum(checks.values()) / len(checks)) if checks else 0.0
    return {
        "case_id": case.get("use_case_id"),
        "tier": case.get("tier"),
        "name": case.get("name"),
        "checks": checks,
        "details": details,
        "passed": passed,
        "pass_rate": round(pass_rate, 3),
        "error": None,
    }


def aggregate(results: list[dict]) -> dict:
    """전체 케이스 결과 → 요약 통계."""
    n = len(results)
    if n == 0:
        return {"total": 0, "passed": 0, "pass_rate": 0.0, "by_tier": {}}
    passed = sum(1 for r in results if r["passed"])
    by_tier: dict[str, dict] = {}
    for r in results:
        t = r.get("tier", "?")
        bucket = by_tier.setdefault(t, {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if r["passed"] else 0
    for t, b in by_tier.items():
        b["pass_rate"] = round(b["passed"] / b["total"], 3) if b["total"] else 0.0
    # check-level 평균(부분 점수) — harness 효과 미세 추적용
    mean_check = round(sum(r["pass_rate"] for r in results) / n, 3)
    return {
        "total": n,
        "passed": passed,
        "pass_rate": round(passed / n, 3),
        "mean_check_pass_rate": mean_check,
        "by_tier": by_tier,
    }


def reduce_runs(case_runs: list[dict]) -> dict:
    """한 케이스의 N회 실행 결과 → 다수결 + 변동성 요약.

    single-run 채점은 LLM 비결정성 때문에 같은 케이스도 실행마다 PASS/FAIL 이
    흔들린다(⑤). N회 돌려 "과반 통과"를 그 케이스의 대표 판정으로 삼고,
    pass_count / pass_ratio / check_rate 평균·범위를 함께 보고해 *실력*과
    *변동성*을 분리한다.

    반환(대표 1건, aggregate 와 호환): passed = (과반 통과). majority 메타 추가.
    """
    runs = [r for r in case_runs if isinstance(r, dict)]
    if not runs:
        return {"passed": False, "pass_rate": 0.0, "checks": {}, "error": "no runs"}
    n = len(runs)
    pass_count = sum(1 for r in runs if r.get("passed"))
    check_rates = [float(r.get("pass_rate", 0.0)) for r in runs]
    # 대표 run = 통과 run 중 첫째(없으면 check_rate 최고) — details 보존용.
    rep = next((r for r in runs if r.get("passed")), max(runs, key=lambda r: r.get("pass_rate", 0.0)))
    majority_pass = pass_count * 2 >= n  # 과반(동률은 통과 측)
    result = dict(rep)
    result["passed"] = majority_pass
    result["pass_rate"] = round(sum(check_rates) / n, 3)
    result["majority"] = {
        "runs": n,
        "pass_count": pass_count,
        "pass_ratio": round(pass_count / n, 3),
        "check_rate_min": round(min(check_rates), 3),
        "check_rate_max": round(max(check_rates), 3),
        "stable": pass_count == 0 or pass_count == n,  # 흔들림 없으면 True
    }
    return result
