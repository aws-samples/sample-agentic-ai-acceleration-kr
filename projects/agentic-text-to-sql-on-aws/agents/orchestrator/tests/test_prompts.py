from orchestrator.mcp_parsing import SchemaHit, SchemaSearchResult, SqlResult
from orchestrator.prompts import (
    SYSTEM_PROMPT,
    build_schema_context,
    build_sql_generation_prompt,
    build_synthesis_prompt,
)


def test_system_prompt_enforces_order_and_safety():
    assert "search_schema" in SYSTEM_PROMPT
    assert "run_sql" in SYSTEM_PROMPT
    assert "SELECT" in SYSTEM_PROMPT
    assert "상상" in SYSTEM_PROMPT  # 스키마에 없는 컬럼 상상 금지
    assert "한국어" in SYSTEM_PROMPT


def test_build_schema_context_with_hits():
    search = SchemaSearchResult(
        results=[
            SchemaHit("column", "orders", "total_amount", "주문 총액", "total_amount NUMERIC", 0.9),
            SchemaHit("table", "orders", None, "주문 헤더", "", 0.8),
        ]
    )
    ctx = build_schema_context(search)
    assert "orders.total_amount" in ctx
    assert "주문 총액" in ctx
    assert "total_amount NUMERIC" in ctx
    assert "orders" in ctx


def test_build_schema_context_empty():
    ctx = build_schema_context(SchemaSearchResult(results=[]))
    assert "재검색" in ctx


def test_build_sql_generation_prompt_initial():
    p = build_sql_generation_prompt("서울 매출은?", "스키마 컨텍스트")
    assert "서울 매출은?" in p
    assert "스키마 컨텍스트" in p
    assert "직전에 생성한 SQL" not in p
    assert "SELECT" in p


def test_build_sql_generation_prompt_with_correction():
    p = build_sql_generation_prompt(
        "서울 매출은?",
        "스키마",
        previous_sql="SELECT * FROM orders",
        failure_feedback="relation orders_x does not exist",
    )
    assert "SELECT * FROM orders" in p
    assert "relation orders_x does not exist" in p
    assert "직전에 생성한 SQL" in p


def test_build_synthesis_prompt_includes_table():
    result = SqlResult(
        status="ok",
        columns=["region", "revenue"],
        rows=[["서울", 1000], ["부산", 500]],
        row_count=2,
    )
    p = build_synthesis_prompt("지역별 매출", "SELECT region, sum(...)", result)
    assert "지역별 매출" in p
    assert "region | revenue" in p
    assert "서울 | 1000" in p


def test_build_synthesis_prompt_truncated_note():
    result = SqlResult(status="ok", columns=["a"], rows=[[1]], row_count=1, truncated=True)
    p = build_synthesis_prompt("q", "SELECT a", result)
    assert "잘렸습니다" in p


def test_synthesis_table_row_cap():
    rows = [[i] for i in range(100)]
    result = SqlResult(status="ok", columns=["n"], rows=rows, row_count=100)
    p = build_synthesis_prompt("q", "SELECT n", result)
    assert "50행만 표시" in p
