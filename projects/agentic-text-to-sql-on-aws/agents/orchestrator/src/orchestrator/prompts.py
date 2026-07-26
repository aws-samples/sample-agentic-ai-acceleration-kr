"""시스템 프롬프트 및 프롬프트 빌더 (순수 로직).

한국어/영어 질의를 모두 처리하고, search_schema → SQL 생성 → run_sql → 요약
순서를 강제한다. 스키마 grounding(존재하지 않는 컬럼 상상 금지)과 SELECT 전용을 명시한다.
프롬프트 문자열 조립은 부수효과가 없어 단위 테스트로 커버한다.
"""

from __future__ import annotations

from .mcp_parsing import SchemaSearchResult, SqlResult

# 단일 Agent(폴백) 모드 및 Graph 노드 공통 시스템 프롬프트.
SYSTEM_PROMPT = """\
당신은 e-커머스 데이터베이스에 대한 자연어 질문을 안전한 PostgreSQL SELECT 쿼리로
변환하고 실행하여 결과를 설명하는 Text-to-SQL 오케스트레이터입니다.

## 언어
- 한국어와 영어 질의를 모두 이해합니다. 최종 답변(요약)은 사용자의 질의 언어에 맞춥니다.
  질의 언어가 불명확하면 한국어로 답합니다.

## 반드시 지킬 작업 순서
1. `search_schema` 도구를 **먼저** 호출해 질문과 관련된 테이블/컬럼/비즈니스 용어를 조회합니다.
2. 조회된 스키마 컨텍스트에만 근거해 PostgreSQL SELECT 쿼리를 작성합니다.
3. `run_sql` 도구로 쿼리를 실행합니다.
4. 실행 결과를 요약합니다. 결과가 표 형태 데이터이면 반드시 GFM markdown 표
   (`| 컬럼 | ... |` 형식)로 정리해 보여주고, 그 아래 한국어 요약을 1~3문장 덧붙입니다.
   행이 20개를 넘으면 상위 20행만 표로 보여주고 잘렸음을 명시합니다.

## SQL 생성 규칙 (엄수)
- **오직 SELECT (또는 WITH ... SELECT) 단일 문장만** 생성합니다.
  INSERT/UPDATE/DELETE/DDL/다중 문장은 절대 생성하지 않습니다.
- `search_schema` 결과의 테이블/컬럼만 사용합니다. **스키마에 없는 컬럼을 상상하지 마세요.**
- 관련 스키마를 못 찾으면, 추측하지 말고 다시 `search_schema` 로 다른 표현을 검색합니다.
- 집계/필터 시 컬럼 설명(COMMENT)의 비즈니스 의미를 따릅니다.
- 대량 결과가 예상되면 적절한 LIMIT 을 포함합니다.

## 오류 대응 (self-correction)
- `run_sql` 이 `rejected`/`error` 를 반환하면, 오류 메시지를 근거로 쿼리를 수정해 재실행합니다.
- 스키마가 불확실하면 `search_schema` 를 재호출해 컨텍스트를 보강한 뒤 재작성합니다.

## 모호한 질의 (M1 정책)
- 질의가 모호해도 되묻지 말고, 가장 합리적인 해석을 선택해 진행하고
  어떤 가정을 했는지 요약에 명시합니다.
"""


def build_schema_context(search: SchemaSearchResult) -> str:
    """search_schema 결과를 SQL 생성 프롬프트에 주입할 텍스트 블록으로 변환."""
    if not search.results:
        return "(관련 스키마를 찾지 못했습니다. 다른 표현으로 재검색이 필요합니다.)"
    lines: list[str] = []
    for hit in search.results:
        target = hit.table if not hit.column else f"{hit.table}.{hit.column}"
        head = f"- [{hit.doc_type}] {target}"
        if hit.description:
            head += f": {hit.description}"
        lines.append(head)
        if hit.ddl_snippet:
            lines.append(f"    DDL: {hit.ddl_snippet}")
    return "\n".join(lines)


def build_sql_generation_prompt(
    question: str,
    schema_context: str,
    previous_sql: str | None = None,
    failure_feedback: str | None = None,
) -> str:
    """SQL 생성/재생성 프롬프트 조립.

    최초 생성이면 previous_sql/failure_feedback 는 None.
    self-correction 재시도면 직전 SQL 과 오류 피드백을 함께 주입한다.
    """
    parts = [
        "다음 스키마 컨텍스트만 사용하여 사용자 질문에 답하는 PostgreSQL SELECT 쿼리를 작성하세요.",
        "",
        "## 스키마 컨텍스트",
        schema_context,
        "",
        "## 사용자 질문",
        question.strip(),
    ]
    if previous_sql:
        parts += ["", "## 직전에 생성한 SQL (실패)", previous_sql.strip()]
    if failure_feedback:
        parts += ["", "## 실패 원인 (이 문제를 반드시 해결하세요)", failure_feedback.strip()]
    parts += [
        "",
        "단일 SELECT 문만, 코드 블록 없이 SQL 만 출력하세요.",
    ]
    return "\n".join(parts)


def build_synthesis_prompt(question: str, sql: str, result: SqlResult) -> str:
    """실행 결과를 바탕으로 한국어 요약을 생성하는 프롬프트 조립."""
    parts = [
        "다음 SQL 실행 결과를 바탕으로 사용자 질문에 답하세요.",
        "결과가 표 형태 데이터이면 반드시 GFM markdown 표(`| 컬럼 | ... |` 형식)로 정리해",
        "보여주고, 그 아래 요약을 1~3문장 덧붙입니다. 행이 20개를 넘으면 상위 20행만",
        "표로 보여주고 잘렸음을 명시하세요.",
        "질의 언어에 맞춰 답하되, 불명확하면 한국어로 답합니다.",
        "",
        "## 사용자 질문",
        question.strip(),
        "",
        "## 실행한 SQL",
        sql.strip(),
        "",
        "## 결과",
        _format_result_table(result),
    ]
    if result.truncated:
        parts.append("\n(주의: 결과가 최대 행 수 제한으로 잘렸습니다. 요약에 이 점을 언급하세요.)")
    return "\n".join(parts)


def _format_result_table(result: SqlResult, max_rows: int = 50) -> str:
    """결과를 프롬프트용 텍스트 표로 변환(과도한 토큰 방지 위해 상한)."""
    if not result.ok:
        return "(결과 없음)"
    if not result.columns:
        return f"(컬럼 없음, row_count={result.row_count})"
    header = " | ".join(result.columns)
    body_rows = result.rows[:max_rows]
    body = "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in body_rows)
    note = ""
    if len(result.rows) > max_rows:
        note = f"\n... (총 {result.row_count}행 중 {max_rows}행만 표시)"
    return f"{header}\n{body}{note}"
