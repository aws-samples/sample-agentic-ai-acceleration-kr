"""트레이스 스팬에서 (질문, 생성 SQL, 상태) 추출 (순수 로직).

AgentCore Evaluations 는 평가 대상 트레이스의 스팬을 `evaluationInput.sessionSpans`
로 넘긴다. 스팬 JSON 의 스키마는 CloudWatch/OTel 계보에 따라 형태가 다양하므로
**방어적으로** 훑는다.

추출 우선순위:
1. `t2sql_query_record` 마커 (§9.5) — orchestrator 가 실행 종료 시 남기는 구조화 로그.
   어떤 문자열 값(body/message/attributes/logs 어디든)에 마커가 있으면 그 뒤의 JSON 을
   파싱한다. 가장 신뢰도 높은 원천이며 version vector 도 여기서 얻는다.
2. gen_ai 계열 속성 — 마커가 없을 때의 폴백.
   질문: `gen_ai.prompt`, `gen_ai.user.message`, `gen_ai.input.messages`, `input.value` 등
   SQL: `gen_ai.tool.input`(run_sql 호출 인자), `db.statement`, `db.query.text` 등

여러 스팬에서 값이 나오면 **가장 마지막(가장 나중 스팬)** 값을 사용한다
(self-correction 루프의 최종 SQL 이 평가 대상).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# §9.5 구조화 로그 마커.
QUERY_RECORD_MARKER = "t2sql_query_record"

# 질문 후보 속성 키 (소문자 비교).
_QUESTION_KEYS = (
    "t2sql.question",
    "gen_ai.prompt",
    "gen_ai.user.message",
    "gen_ai.input.messages",
    "gen_ai.request.prompt",
    "input.value",
    "question",
    "user_input",
)

# SQL 후보 속성 키 (소문자 비교).
_SQL_KEYS = (
    "t2sql.sql",
    "db.statement",
    "db.query.text",
    "sql",
    "gen_ai.tool.input",
    "tool.input",
)

# 중첩 탐색 상한 (6MB 입력 방어 — 병적으로 깊은 구조에서 폭주 방지).
_MAX_DEPTH = 12

_SQL_TEXT_RE = re.compile(r"\b(select|with)\b", re.IGNORECASE)


@dataclass
class ExtractedRun:
    """스팬에서 복원한 실행 1건."""

    question: str | None = None
    sql: str | None = None
    status: str | None = None
    session_id: str | None = None
    version: dict[str, Any] = field(default_factory=dict)
    # t2sql_query_record 마커에서 나온 값인지(신뢰도 구분/설명 문구용).
    from_query_record: bool = False

    def has_minimum(self) -> bool:
        """평가를 시도할 최소 정보(질문 + SQL)가 있는지."""
        return bool(self.question and self.sql)


def extract_run(evaluation_input: Any) -> ExtractedRun:
    """`evaluationInput`(또는 sessionSpans 리스트)에서 실행 1건을 복원."""
    spans = _session_spans(evaluation_input)
    run = ExtractedRun()

    # 1차: t2sql_query_record 마커 (가장 신뢰도 높음, 마지막 레코드 우선).
    records = [rec for span in spans for rec in _find_query_records(span)]
    if records:
        record = records[-1]
        run.question = _clean_str(record.get("question"))
        run.sql = _clean_str(record.get("sql"))
        run.status = _clean_str(record.get("status"))
        run.session_id = _clean_str(record.get("session_id"))
        version = record.get("version")
        run.version = dict(version) if isinstance(version, dict) else {}
        run.from_query_record = True
        if run.has_minimum():
            return run

    # 2차: gen_ai/db 속성 폴백 (마커가 없거나 불완전할 때만 빈 필드를 채운다).
    fallback = _extract_from_attributes(spans)
    run.question = run.question or fallback.question
    run.sql = run.sql or fallback.sql
    run.status = run.status or fallback.status
    run.session_id = run.session_id or fallback.session_id
    return run


# --- 내부 헬퍼 -------------------------------------------------------------


def _session_spans(evaluation_input: Any) -> list[Any]:
    """evaluationInput 에서 sessionSpans 리스트를 방어적으로 추출."""
    if isinstance(evaluation_input, list):
        return evaluation_input
    if isinstance(evaluation_input, dict):
        for key in ("sessionSpans", "session_spans", "spans"):
            value = evaluation_input.get(key)
            if isinstance(value, list):
                return value
        # 단일 스팬 객체가 그대로 온 경우.
        return [evaluation_input]
    return []


def _find_query_records(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """중첩 구조를 훑어 마커 뒤의 JSON 객체를 모두 파싱해 반환."""
    if depth > _MAX_DEPTH:
        return []
    found: list[dict[str, Any]] = []
    if isinstance(node, str):
        if QUERY_RECORD_MARKER in node:
            record = _parse_marked_json(node)
            if record is not None:
                found.append(record)
        return found
    if isinstance(node, dict):
        for value in node.values():
            found.extend(_find_query_records(value, depth + 1))
        return found
    if isinstance(node, (list, tuple)):
        for item in node:
            found.extend(_find_query_records(item, depth + 1))
    return found


def _parse_marked_json(text: str) -> dict[str, Any] | None:
    """`... t2sql_query_record {json}` 형태에서 JSON 객체를 파싱."""
    idx = text.find(QUERY_RECORD_MARKER)
    if idx == -1:
        return None
    tail = text[idx + len(QUERY_RECORD_MARKER) :]
    start = tail.find("{")
    if start == -1:
        return None
    candidate = _balanced_object(tail[start:])
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _balanced_object(text: str) -> str | None:
    """첫 `{` 부터 중괄호 균형이 맞는 지점까지 잘라낸다(문자열 리터럴 고려)."""
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[: i + 1]
    return None


def _extract_from_attributes(spans: list[Any]) -> ExtractedRun:
    """gen_ai/db 속성에서 질문·SQL 을 폴백 추출 (마지막 값 우선)."""
    run = ExtractedRun()
    for span in spans:
        for key, value in _iter_scalar_pairs(span):
            lowered = key.lower()
            if lowered in _QUESTION_KEYS:
                text = _clean_str(_maybe_message_text(value))
                if text:
                    run.question = text
            elif lowered in _SQL_KEYS:
                text = _clean_str(_maybe_sql_text(value))
                if text:
                    run.sql = text
            elif lowered in ("session.id", "session_id", "sessionid"):
                run.session_id = _clean_str(value) or run.session_id
    return run


def _iter_scalar_pairs(node: Any, depth: int = 0):
    """중첩 dict/list 를 훑어 (키, 스칼라값) 쌍을 순서대로 방출."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list, tuple)):
                yield from _iter_scalar_pairs(value, depth + 1)
                # OTel attribute 형태({"key": "...", "value": {"stringValue": ...}}) 대응.
                if key == "value" and isinstance(node.get("key"), str):
                    flattened = _flatten_otel_value(value)
                    if flattened is not None:
                        yield (str(node["key"]), flattened)
            else:
                yield (str(key), value)
                if key == "value" and isinstance(node.get("key"), str):
                    yield (str(node["key"]), value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_scalar_pairs(item, depth + 1)


def _flatten_otel_value(value: Any) -> Any:
    """OTel AnyValue({"stringValue": ...} 등)에서 스칼라를 꺼낸다."""
    if isinstance(value, dict):
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in value:
                return value[key]
    return None


def _maybe_message_text(value: Any) -> Any:
    """질문 속성이 메시지 배열 JSON 문자열이면 마지막 user 텍스트를 꺼낸다."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped.startswith(("[", "{")):
        return value
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return value
    return _last_user_text(parsed) or value


def _last_user_text(parsed: Any) -> str | None:
    messages = parsed if isinstance(parsed, list) else [parsed]
    texts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "user")).lower() != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    return texts[-1] if texts else None


def _maybe_sql_text(value: Any) -> Any:
    """SQL 속성이 도구 인자 JSON(`{"sql": "...")`)이면 sql 필드를 꺼낸다."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return value
        if isinstance(parsed, dict):
            for key in ("sql", "query", "statement"):
                candidate = parsed.get(key)
                if isinstance(candidate, str) and _SQL_TEXT_RE.search(candidate):
                    return candidate
        return value
    return value if _SQL_TEXT_RE.search(stripped) else None


def _clean_str(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return None
    text = str(value).strip()
    return text or None
