"""결과셋 정규화 비교 + 경량 SQL 방어 (순수 로직).

## 결과셋 비교 (Execution Accuracy)
- 컬럼명은 무시한다(별칭 차이를 정답 판정에 반영하지 않음).
- 행은 값 튜플의 **multiset(Counter)** 으로 비교 → 순서 무시, 중복 행은 개수까지 비교.
- 숫자는 float 로 캐스팅해 소수 6자리 반올림(Data API 의 Decimal/문자열 표현 차이 흡수).
- 컬럼 개수가 다르면 불일치.

> multiset 을 쓰는 이유: 단순 set 이면 중복 행이 다른 두 결과가 같다고 판정된다.

## 경량 SQL 방어 (READ-ONLY 4중 방어의 보조)
sqlglot 의존 없이 표준 lib 만 사용한다(Lambda 의존성 zip 회피). 최후 방어선은
read-only(agent_ro) DB grant 이며, 여기서는 명백한 위반만 조기에 막는다.
- 단일 statement (문자열 리터럴/주석 밖의 `;` 로 분리되는 두 번째 statement 금지)
- `SELECT` 또는 `WITH` 로 시작
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

# 숫자 반올림 자릿수.
FLOAT_PRECISION = 6

# 명백한 쓰기/DDL 키워드 (조기 거부용 — 최후 방어는 DB grant).
_FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "truncate",
    "grant",
    "revoke",
    "copy",
    "call",
    "merge",
    "vacuum",
)

_WORD_RE = re.compile(r"[a-z_]+")


class UnsafeSqlError(ValueError):
    """경량 방어에서 거부된 SQL."""


@dataclass(frozen=True)
class ComparisonResult:
    """결과셋 비교 결과."""

    matched: bool
    summary: str


def strip_sql_noise(sql: str) -> str:
    """주석(`--`, `/* */`)과 문자열 리터럴 내용을 제거한 검사용 사본을 만든다."""
    out: list[str] = []
    i = 0
    length = len(sql)
    while i < length:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < length else ""
        if ch == "-" and nxt == "-":
            while i < length and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < length and not (sql[i] == "*" and i + 1 < length and sql[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == "'":
            # 문자열 리터럴은 내용 제거(''  이스케이프 처리).
            i += 1
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
            continue
        if ch == '"':
            i += 1
            while i < length and sql[i] != '"':
                i += 1
            i += 1
            out.append('""')
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def assert_safe_select(sql: str) -> str:
    """생성 SQL 의 경량 안전성 검사. 통과 시 trailing `;` 을 제거한 SQL 반환.

    실패 시 :class:`UnsafeSqlError`.
    """
    if not sql or not sql.strip():
        raise UnsafeSqlError("SQL 이 비어 있습니다.")
    cleaned = strip_sql_noise(sql)
    # 단일 statement 검증: 마지막 세미콜론 뒤에 실질 토큰이 없어야 한다.
    segments = [seg for seg in cleaned.split(";") if seg.strip()]
    if len(segments) > 1:
        raise UnsafeSqlError("여러 statement 는 허용되지 않습니다(단일 SELECT 만).")
    lowered = cleaned.strip().lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise UnsafeSqlError("SELECT 또는 WITH 로 시작하는 조회문만 허용됩니다.")
    words = set(_WORD_RE.findall(lowered))
    hits = [kw for kw in _FORBIDDEN_KEYWORDS if kw in words]
    if hits:
        raise UnsafeSqlError(f"허용되지 않는 키워드 포함: {', '.join(sorted(hits))}")
    return sql.strip().rstrip(";").strip()


def normalize_value(value: Any) -> Any:
    """단일 값 정규화. 숫자는 float 반올림, 그 외는 문자열(None 은 그대로)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), FLOAT_PRECISION)
    text = str(value).strip()
    # Data API 가 NUMERIC 을 문자열로 주는 경우까지 숫자로 흡수.
    try:
        return round(float(text), FLOAT_PRECISION)
    except (TypeError, ValueError):
        return text


def normalize_rows(rows: list[list[Any]]) -> Counter:
    """행 목록을 값 튜플 multiset 으로 정규화(컬럼명 무시·순서 무시)."""
    return Counter(tuple(normalize_value(v) for v in row) for row in rows or [])


def compare_result_sets(
    gold_rows: list[list[Any]],
    pred_rows: list[list[Any]],
) -> ComparisonResult:
    """gold 와 생성 SQL 결과셋을 정규화 비교하고 차이를 요약한다."""
    gold_widths = {len(r) for r in gold_rows or []}
    pred_widths = {len(r) for r in pred_rows or []}
    if gold_widths and pred_widths and gold_widths != pred_widths:
        return ComparisonResult(
            matched=False,
            summary=(
                f"컬럼 개수 불일치 (gold={sorted(gold_widths)}, "
                f"predicted={sorted(pred_widths)})"
            ),
        )

    gold_counter = normalize_rows(gold_rows)
    pred_counter = normalize_rows(pred_rows)
    if gold_counter == pred_counter:
        return ComparisonResult(
            matched=True,
            summary=f"결과셋 일치 ({sum(gold_counter.values())}행)",
        )

    missing = gold_counter - pred_counter
    extra = pred_counter - gold_counter
    parts = [
        f"행 수 gold={sum(gold_counter.values())} predicted={sum(pred_counter.values())}",
    ]
    if missing:
        parts.append(f"누락 {sum(missing.values())}행 예: {_sample(missing)}")
    if extra:
        parts.append(f"초과 {sum(extra.values())}행 예: {_sample(extra)}")
    return ComparisonResult(matched=False, summary="결과셋 불일치 — " + "; ".join(parts))


def _sample(counter: Counter, limit: int = 2) -> str:
    """차이 요약에 넣을 샘플 행 문자열(길이 제한)."""
    samples = []
    for row, _count in list(counter.items())[:limit]:
        text = str(list(row))
        samples.append(text[:120] + ("…" if len(text) > 120 else ""))
    return " | ".join(samples)
