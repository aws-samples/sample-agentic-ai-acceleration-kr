"""Execution Accuracy(EX) code-based evaluator Lambda 핸들러.

AgentCore Evaluations 의 custom **code-based evaluator** 계약을 구현한다
(CLAUDE.md 의 "Tool layer 에 Lambda 금지" 제약의 명시적 예외 — Evaluations 서비스 규격).

## Lambda 계약
입력 이벤트:
```
{"schemaVersion":"1.0","evaluatorId":...,"evaluatorName":...,
 "evaluationLevel":"TRACE",
 "evaluationInput":{"sessionSpans":[...]},
 "evaluationReferenceInputs":[...],
 "evaluationTarget":{"traceIds":[...],"spanIds":[...]}}
```
성공 응답: ``{"label":"PASS"|"FAIL"|"SKIP","value":1.0|0.0,"explanation":"..."}``
오류 응답: ``{"errorCode":"...","errorMessage":"..."}``
제약: 최대 300초, 입력 최대 6MB.

## 평가 로직 (§9.1)
1. `sessionSpans` 에서 `t2sql_query_record`(§9.5) 또는 gen_ai 속성으로 (질문, SQL, status) 복원
2. goldset(`goldset-v1.jsonl`) 질문 매칭 — 매칭 없으면 **SKIP**
3. gold SQL / 생성 SQL 을 read-only(agent_ro)로 각각 실행해 결과셋 정규화 비교
   → 일치 PASS/1.0, 불일치 FAIL/0.0 (+차이 요약)
4. 생성 SQL 은 실행 전 경량 방어(단일 statement + SELECT/WITH) — 위반 시 FAIL
5. explanation 에 `EVALUATOR_VERSION`(기본 1.0.0)·goldset id·version vector 병기

환경변수: `AURORA_CLUSTER_ARN`, `AURORA_SECRET_ARN`, `DB_NAME`, `EVALUATOR_VERSION`,
`GOLDSET_PATH`(선택), `AWS_REGION`(기본 us-west-2).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .comparison import UnsafeSqlError, assert_safe_select, compare_result_sets
from .dataapi import AuroraReadOnlyRunner, DataApiError
from .goldset import GoldsetMatcher
from .spans import extract_run

logger = logging.getLogger("evaluation.handler")
logging.getLogger().setLevel(logging.INFO)

DEFAULT_EVALUATOR_VERSION = "1.0.0"

LABEL_PASS = "PASS"
LABEL_FAIL = "FAIL"
LABEL_SKIP = "SKIP"

ERROR_INVALID_INPUT = "InvalidEvaluationInput"
ERROR_DATASOURCE = "DataSourceExecutionError"
ERROR_INTERNAL = "InternalEvaluatorError"

# 콜드스타트 간 재사용(goldset 파일 재파싱 회피).
_MATCHER: GoldsetMatcher | None = None
_RUNNER: AuroraReadOnlyRunner | None = None


def _get_matcher() -> GoldsetMatcher:
    global _MATCHER
    if _MATCHER is None:
        path = os.environ.get("GOLDSET_PATH") or None
        from .goldset import load_goldset

        _MATCHER = GoldsetMatcher(load_goldset(path))
    return _MATCHER


def _get_runner() -> AuroraReadOnlyRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = AuroraReadOnlyRunner()
    return _RUNNER


def evaluator_version() -> str:
    return os.environ.get("EVALUATOR_VERSION") or DEFAULT_EVALUATOR_VERSION


def handler(
    event: dict[str, Any],
    context: Any = None,
    *,
    matcher: GoldsetMatcher | None = None,
    runner: AuroraReadOnlyRunner | None = None,
) -> dict[str, Any]:
    """code-based evaluator 진입점.

    matcher/runner 는 단위 테스트용 주입 지점이다(미주입 시 env 기반 생성).
    어떤 예외도 밖으로 던지지 않고 계약상 오류 응답으로 정규화한다.
    """
    try:
        return _evaluate(event or {}, matcher=matcher, runner=runner)
    except Exception as exc:  # noqa: BLE001 — 계약상 오류 응답으로 정규화
        logger.exception("평가 중 예상치 못한 오류")
        return _error(ERROR_INTERNAL, f"평가 중 오류가 발생했습니다: {exc}")


def _evaluate(
    event: dict[str, Any],
    *,
    matcher: GoldsetMatcher | None,
    runner: AuroraReadOnlyRunner | None,
) -> dict[str, Any]:
    version = evaluator_version()
    evaluation_input = event.get("evaluationInput") or event.get("evaluation_input")
    if evaluation_input is None:
        return _error(ERROR_INVALID_INPUT, "evaluationInput 이 없습니다.")

    run = extract_run(evaluation_input)
    version_note = _version_note(run.version)

    if not run.question:
        return _skip(
            "스팬에서 질문을 추출할 수 없어 평가를 건너뜁니다.", version, version_note
        )

    # orchestrator 가 clarification 으로 끝난 실행은 SQL 이 없다 → 평가 대상 아님.
    if run.status == "clarification":
        return _skip(
            "clarification 으로 종료된 실행이라 평가 대상이 아닙니다.", version, version_note
        )

    active_matcher = matcher if matcher is not None else _get_matcher()
    gold = active_matcher.match(run.question)
    if gold is None:
        return _skip(
            f"goldset 에 매칭되는 질문이 없습니다(질문: {_truncate(run.question)}).",
            version,
            version_note,
        )

    if not run.sql:
        return _fail(
            f"goldset={gold.id} 매칭됐지만 생성 SQL 이 없습니다"
            f"(실행 상태: {run.status or '알 수 없음'}).",
            version,
            version_note,
        )

    if gold.datasource != "aurora":
        return _skip(
            f"goldset={gold.id} 의 datasource={gold.datasource} 는 EX 비교 대상이 아닙니다.",
            version,
            version_note,
        )

    # 경량 방어: 단일 statement + SELECT/WITH (최후 방어는 read-only DB grant).
    try:
        predicted_sql = assert_safe_select(run.sql)
    except UnsafeSqlError as exc:
        return _fail(
            f"goldset={gold.id} — 생성 SQL 이 안전 검사에서 거부되었습니다: {exc}",
            version,
            version_note,
        )

    active_runner = runner if runner is not None else _get_runner()
    try:
        gold_result = active_runner.run(gold.sql.rstrip(";").strip())
    except DataApiError as exc:
        # gold SQL 실패는 goldset/환경 문제 → 평가 오류(에이전트 책임 아님).
        return _error(ERROR_DATASOURCE, f"gold SQL 실행 실패(goldset={gold.id}): {exc}")

    try:
        pred_result = active_runner.run(predicted_sql)
    except DataApiError as exc:
        # 생성 SQL 실행 실패는 EX 기준 오답.
        return _fail(
            f"goldset={gold.id} — 생성 SQL 실행 실패: {exc}",
            version,
            version_note,
        )

    comparison = compare_result_sets(gold_result.rows, pred_result.rows)
    detail = f"goldset={gold.id} — {comparison.summary}"
    if comparison.matched:
        return _pass(detail, version, version_note)
    return _fail(detail, version, version_note)


# --- 응답 빌더 -------------------------------------------------------------


def _pass(detail: str, version: str, version_note: str) -> dict[str, Any]:
    return {
        "label": LABEL_PASS,
        "value": 1.0,
        "explanation": _explanation(detail, version, version_note),
    }


def _fail(detail: str, version: str, version_note: str) -> dict[str, Any]:
    return {
        "label": LABEL_FAIL,
        "value": 0.0,
        "explanation": _explanation(detail, version, version_note),
    }


def _skip(detail: str, version: str, version_note: str) -> dict[str, Any]:
    # 계약상 label 은 필수. value 는 SKIP 에서 생략한다(집계 왜곡 방지).
    return {
        "label": LABEL_SKIP,
        "explanation": _explanation(detail, version, version_note),
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {"errorCode": code, "errorMessage": message}


def _explanation(detail: str, version: str, version_note: str) -> str:
    parts = [f"[EX evaluator v{version}]", detail]
    if version_note:
        parts.append(f"({version_note})")
    return " ".join(parts)


def _version_note(version: dict[str, Any]) -> str:
    """orchestrator version vector 를 explanation 용 문자열로."""
    if not version:
        return ""
    bundle = version.get("bundle")
    agent = version.get("agent")
    bits = []
    if bundle:
        bits.append(f"bundle={bundle}")
    if agent:
        bits.append(f"agent={agent}")
    return ", ".join(bits)


def _truncate(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
