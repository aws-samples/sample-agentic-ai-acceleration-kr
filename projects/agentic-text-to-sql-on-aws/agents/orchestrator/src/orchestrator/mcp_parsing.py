"""MCP 도구 응답 파싱 (순수 로직).

sql-execution-mcp 의 `run_sql`, semantic-retrieval-mcp 의 `search_schema` 응답을
구조화된 dataclass 로 변환한다. MCP 도구는 텍스트(JSON 문자열) 또는 dict 를 반환할 수
있으므로 두 형태를 모두 수용한다. 부수효과가 없어 단위 테스트로 완전 커버한다.

계약 (변경 금지):
- run_sql(sql) ->
    {"status":"ok","columns","rows","row_count","truncated"}
  | {"status":"rejected","reason","rule"}
  | {"status":"error","message"}
- search_schema(query, top_k=5) ->
    {"results":[{doc_type,table,column,description,ddl_snippet,score}]}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SqlResult:
    """`run_sql` 응답."""

    status: str  # "ok" | "rejected" | "error"
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    reason: str | None = None  # rejected 시 사유
    rule: str | None = None  # rejected 시 위반 규칙
    message: str | None = None  # error 시 메시지

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def needs_correction(self) -> bool:
        """self-correction 루프를 돌려야 하는 상태인지."""
        return self.status in ("rejected", "error")

    def failure_feedback(self) -> str:
        """SQL 재생성 프롬프트에 주입할 오류 피드백 문자열."""
        if self.status == "rejected":
            rule = f" (위반 규칙: {self.rule})" if self.rule else ""
            return f"이전 SQL 이 거부되었습니다{rule}: {self.reason or '사유 미상'}"
        if self.status == "error":
            return f"이전 SQL 실행 중 오류가 발생했습니다: {self.message or '메시지 없음'}"
        return ""


@dataclass(frozen=True)
class SchemaHit:
    """`search_schema` 결과 항목."""

    doc_type: str
    table: str
    column: str | None
    description: str
    ddl_snippet: str
    score: float


@dataclass(frozen=True)
class SchemaSearchResult:
    """`search_schema` 응답."""

    results: list[SchemaHit] = field(default_factory=list)


def _coerce_payload(payload: Any) -> dict[str, Any]:
    """MCP 반환값(dict | JSON 문자열 | {'text': ...} 래핑)을 dict 로 정규화."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"MCP 응답 JSON 파싱 실패: {payload!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"MCP 응답이 객체가 아닙니다: {type(payload).__name__}")
    return payload


def parse_sql_result(payload: Any) -> SqlResult:
    """`run_sql` 응답을 SqlResult 로 파싱."""
    data = _coerce_payload(payload)
    status = str(data.get("status", "")).strip().lower()
    if status == "ok":
        return SqlResult(
            status="ok",
            columns=list(data.get("columns") or []),
            rows=[list(r) for r in (data.get("rows") or [])],
            row_count=int(data.get("row_count", len(data.get("rows") or []))),
            truncated=bool(data.get("truncated", False)),
        )
    if status == "rejected":
        return SqlResult(
            status="rejected",
            reason=_as_str(data.get("reason")),
            rule=_as_str(data.get("rule")),
        )
    if status == "error":
        return SqlResult(status="error", message=_as_str(data.get("message")))
    # 알 수 없는 상태 → 오류로 취급(방어적).
    return SqlResult(
        status="error",
        message=f"알 수 없는 run_sql status: {status!r} (원본: {data!r})",
    )


def parse_schema_search(payload: Any) -> SchemaSearchResult:
    """`search_schema` 응답을 SchemaSearchResult 로 파싱."""
    data = _coerce_payload(payload)
    hits: list[SchemaHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        hits.append(
            SchemaHit(
                doc_type=_as_str(item.get("doc_type")) or "",
                table=_as_str(item.get("table")) or "",
                column=_as_str(item.get("column")),
                description=_as_str(item.get("description")) or "",
                ddl_snippet=_as_str(item.get("ddl_snippet")) or "",
                score=_as_float(item.get("score")),
            )
        )
    return SchemaSearchResult(results=hits)


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
