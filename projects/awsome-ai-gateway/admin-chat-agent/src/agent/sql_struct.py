# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""sql_struct — SQL 의 결정적 구조 사실 추출 (DEVLOG §58, L2).

ask_validator 가 'SQL 텍스트 + 20행 샘플'만 보고 의도부합을 LLM 으로 *추정*하던
PASS 편향(감사 결함: validator 가 틀린 집계를 PASS)을 억제하기 위해, sqlglot AST
에서 검증가능한 구조 사실(참조 테이블·JOIN 키·집계 함수와 대상 컬럼·GROUP BY 키·
WHERE 술어·COUNT(DISTINCT) 유무·timezone 앵커 유무)을 결정적으로 추출한다.

validator 는 이 fact sheet 를 받아 '문장이 그럴듯한가'가 아니라 '구조가 의도와
맞는가'를 항목별 rubric 으로 채점한다(G-Eval/structured-judge 의 SQL 특화).

sqlglot 외 의존성 없음. 추출 실패는 빈 dict 로 graceful(검증을 막지 않음).
"""

from __future__ import annotations

from typing import Any

try:
    import sqlglot
    import sqlglot.expressions as exp
except ImportError:  # pragma: no cover
    sqlglot = None
    exp = None


def _alias_to_table(ast: Any) -> dict[str, str]:
    """alias-or-name(소문자) -> 'schema.table'."""
    m: dict[str, str] = {}
    for t in ast.find_all(exp.Table):
        sch = (t.text("db") or "").lower()
        name = (t.name or "").lower()
        fq = f"{sch}.{name}" if sch else name
        m[(t.alias or name).lower()] = fq
    return m


def _col_fq(col: Any, alias_map: dict[str, str]) -> str:
    tbl = (col.table or "").lower()
    name = (col.name or "").lower()
    if tbl in alias_map:
        return f"{alias_map[tbl]}.{name}"
    return f"{tbl}.{name}".strip(".") if tbl else name


def extract_facts(sql: str) -> dict[str, Any]:
    """SQL → 구조 사실 dict. 실패 시 {'_parse_ok': False}.

    반환 키:
      tables: ["usage.usage_logs", ...]
      joins: [{"table": ..., "on": "...", "via_pk": bool}]
      aggregates: [{"func": "SUM", "column": "usage.usage_logs.cost_usd", "distinct": bool}]
      group_by: ["auth.users.email", ...]
      where_columns: ["usage.usage_logs.status", ...]
      filters: ["status = 'SUCCESS'", ...]   # 술어 텍스트
      has_count_distinct: bool
      timestamptz_anchored: bool | None       # 시간필터가 AT TIME ZONE 으로 감싸졌나
      has_time_filter: bool
    """
    if sqlglot is None:
        return {"_parse_ok": False}
    try:
        ast = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:  # noqa: BLE001
        return {"_parse_ok": False}
    if ast is None:
        return {"_parse_ok": False}

    alias_map = _alias_to_table(ast)
    facts: dict[str, Any] = {"_parse_ok": True}

    # tables
    tables = []
    for t in ast.find_all(exp.Table):
        sch = (t.text("db") or "").lower()
        name = (t.name or "").lower()
        fq = f"{sch}.{name}" if sch else name
        if fq not in tables:
            tables.append(fq)
    facts["tables"] = tables

    # joins
    joins = []
    for j in ast.find_all(exp.Join):
        on = j.args.get("on")
        jt = j.find(exp.Table)
        joins.append(
            {
                "table": (f"{(jt.text('db') or '').lower()}.{(jt.name or '').lower()}".strip(".") if jt else "?"),
                "on": on.sql(dialect="postgres") if on else None,
            }
        )
    facts["joins"] = joins

    # aggregates
    aggs = []
    agg_types = {
        exp.Sum: "SUM",
        exp.Avg: "AVG",
        exp.Count: "COUNT",
        exp.Min: "MIN",
        exp.Max: "MAX",
    }
    has_count_distinct = False
    for agg in ast.find_all(exp.AggFunc):
        fname = agg_types.get(type(agg), type(agg).__name__.upper())
        distinct = bool(agg.args.get("distinct")) or isinstance(agg.this, exp.Distinct)
        cols = [_col_fq(c, alias_map) for c in agg.find_all(exp.Column)]
        if fname == "COUNT" and distinct:
            has_count_distinct = True
        aggs.append(
            {"func": fname, "columns": cols, "distinct": distinct}
        )
    facts["aggregates"] = aggs
    facts["has_count_distinct"] = has_count_distinct

    # group by
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    gb = []
    if select is not None:
        group = select.args.get("group")
        if group is not None:
            for e in group.find_all(exp.Column):
                gb.append(_col_fq(e, alias_map))
    facts["group_by"] = gb

    # where columns + filter texts
    where_cols: list[str] = []
    filters: list[str] = []
    where = select.args.get("where") if select is not None else None
    if where is not None:
        for c in where.find_all(exp.Column):
            fq = _col_fq(c, alias_map)
            if fq not in where_cols:
                where_cols.append(fq)
        # 술어 텍스트(EQ/IN 등) — 짧게
        for pred in where.find_all((exp.EQ, exp.In, exp.GTE, exp.LTE, exp.GT, exp.LT)):
            try:
                filters.append(pred.sql(dialect="postgres"))
            except Exception:  # noqa: BLE001
                continue
    facts["where_columns"] = where_cols
    facts["filters"] = filters[:8]

    # timezone anchoring on time filters
    has_atz = bool(list(ast.find_all(exp.AtTimeZone)))
    has_time_filter = bool(
        list(ast.find_all(exp.Interval))
        or list(ast.find_all(exp.DateTrunc))
        or any((fn.name or "").lower() == "now" for fn in ast.find_all(exp.Anonymous))
        or list(ast.find_all(exp.CurrentTimestamp))
    )
    facts["has_time_filter"] = has_time_filter
    facts["timestamptz_anchored"] = (has_atz if has_time_filter else None)

    return facts


def facts_to_prompt(facts: dict[str, Any]) -> str:
    """fact sheet → validator 프롬프트에 넣을 사람 읽기용 구조 요약."""
    if not facts.get("_parse_ok"):
        return "(SQL 구조 파싱 실패 — 텍스트만으로 판정)"
    lines = []
    lines.append(f"- 참조 테이블: {', '.join(facts.get('tables') or []) or '(없음)'}")
    joins = facts.get("joins") or []
    if joins:
        lines.append("- JOIN:")
        for j in joins:
            lines.append(f"    · {j.get('table')} ON {j.get('on')}")
    aggs = facts.get("aggregates") or []
    if aggs:
        lines.append("- 집계:")
        for a in aggs:
            d = " DISTINCT" if a.get("distinct") else ""
            lines.append(f"    · {a.get('func')}{d}({', '.join(a.get('columns') or [])})")
    lines.append(f"- GROUP BY: {', '.join(facts.get('group_by') or []) or '(없음)'}")
    lines.append(f"- WHERE 컬럼: {', '.join(facts.get('where_columns') or []) or '(없음)'}")
    if facts.get("filters"):
        lines.append(f"- 필터 술어: {' ; '.join(facts['filters'])}")
    lines.append(f"- COUNT(DISTINCT) 사용: {'예' if facts.get('has_count_distinct') else '아니오'}")
    if facts.get("has_time_filter"):
        anchored = facts.get("timestamptz_anchored")
        lines.append(
            f"- 시간 필터 KST 앵커(AT TIME ZONE 'Asia/Seoul'): "
            f"{'있음' if anchored else '없음 ⚠️ (KST 자정과 9시간 어긋날 수 있음)'}"
        )
    return "\n".join(lines)
