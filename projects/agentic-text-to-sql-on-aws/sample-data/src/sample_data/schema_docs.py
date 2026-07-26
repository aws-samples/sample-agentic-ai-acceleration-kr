"""스키마 메타데이터 → semantic 검색 문서 빌더.

테이블/컬럼 메타데이터(이름, 타입, COMMENT, DDL 스니펫, 예시 값)를
OpenSearch 인덱싱용 문서로 변환한다. AWS 호출과 분리되어 단위 테스트 가능.

문서는 두 레벨로 생성한다.
- table 레벨: 테이블 개요(컬럼 목록 요약 포함)
- column 레벨: 개별 컬럼(타입/코멘트/예시 값)

`content` 필드가 임베딩/BM25 대상 텍스트이며, 나머지는 필터/표시용 메타.
"""

from __future__ import annotations

from typing import Any

from sample_data import generator, schema


def _sample_values(dataset: generator.Dataset | None, table: str, column: str,
                   limit: int = 5) -> list[str]:
    """데이터셋에서 컬럼 예시 값 추출(중복 제거, 결정적 순서)."""
    if dataset is None:
        return []
    rows = getattr(dataset, table, [])
    seen: list[str] = []
    for row in rows:
        val = row.get(column)
        if val is None:
            continue
        s = str(val)
        if s not in seen:
            seen.append(s)
        if len(seen) >= limit:
            break
    return seen


def _doc_id(*parts: str) -> str:
    return ".".join(parts)


def build_column_document(
    table: schema.Table,
    column: schema.Column,
    dataset: generator.Dataset | None = None,
) -> dict[str, Any]:
    samples = _sample_values(dataset, table.name, column.name)
    fk = f" 외래키: {column.references}." if column.references else ""
    sample_text = f" 예시 값: {', '.join(samples)}." if samples else ""
    content = (
        f"테이블 {table.name} 의 컬럼 {column.name} ({column.type}). "
        f"{column.comment}{fk}{sample_text}"
    )
    return {
        "doc_id": _doc_id(table.name, column.name),
        "doc_type": "column",
        "table": table.name,
        "column": column.name,
        "data_type": column.type,
        "comment": column.comment,
        "references": column.references,
        "sample_values": samples,
        "ddl_snippet": column.ddl(),
        "content": content,
    }


def build_table_document(
    table: schema.Table,
    dataset: generator.Dataset | None = None,
) -> dict[str, Any]:
    col_summ = "; ".join(f"{c.name}({c.type}) {c.comment}" for c in table.columns)
    content = (
        f"테이블 {table.name}: {table.comment} "
        f"컬럼: {col_summ}"
    )
    row_count = len(getattr(dataset, table.name, [])) if dataset else None
    return {
        "doc_id": _doc_id(table.name),
        "doc_type": "table",
        "table": table.name,
        "column": None,
        "comment": table.comment,
        "columns": [c.name for c in table.columns],
        "row_count": row_count,
        "ddl_snippet": table.create_ddl(),
        "content": content,
    }


def build_documents(
    dataset: generator.Dataset | None = None,
) -> list[dict[str, Any]]:
    """전체 스키마 문서(table + column) 목록 생성. 결정적 순서."""
    docs: list[dict[str, Any]] = []
    for table in schema.TABLES:
        docs.append(build_table_document(table, dataset))
        for column in table.columns:
            docs.append(build_column_document(table, column, dataset))
    return docs
