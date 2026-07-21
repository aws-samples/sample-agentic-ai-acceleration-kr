"""
ecommerce_mcp.py - 이커머스 사내 데이터에 접근하는 stdio MCP 서버.

Claude Code에서는 `.mcp.json`에 아래처럼 등록해서 썼습니다:

    {
      "mcpServers": {
        "ecommerce": {
          "type": "stdio",
          "command": "python",
          "args": ["local_agent/mcp_server/ecommerce_mcp.py"]
        }
      }
    }

도구 3개를 노출합니다 - 모두 read-only:
  • query_sales   : 기간/카테고리/지역별 매출 집계
  • top_products  : 매출 상위 상품 N개
  • run_sql       : 임의 SELECT 쿼리 (분석가용 탈출구)

이 파일의 핵심은 "도구 정의(스키마 + 핸들러)" 입니다. 뒤에서 이 똑같은
도구 정의를 AgentCore Gateway의 Lambda 타깃으로 그대로 옮깁니다.
"""
import os
import sqlite3

from mcp.server.fastmcp import FastMCP

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "ecommerce.db")
mcp = FastMCP("ecommerce")


def _conn():
    # 읽기 전용으로 열어 도구가 데이터를 변경할 수 없게 한다.
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


@mcp.tool()
def query_sales(start_date: str = "2026-01-01", end_date: str = "2026-12-31",
                category: str = "", region: str = "") -> str:
    """기간/카테고리/지역으로 필터링한 매출·주문 집계를 반환합니다.
    날짜는 YYYY-MM-DD. category/region을 비우면 전체."""
    where = ["order_date BETWEEN ? AND ?"]
    params = [start_date, end_date]
    if category:
        where.append("category = ?"); params.append(category)
    if region:
        where.append("region = ?"); params.append(region)
    sql = (f"SELECT COUNT(*) orders, SUM(revenue) revenue, SUM(quantity) units "
           f"FROM orders WHERE {' AND '.join(where)}")
    with _conn() as con:
        r = con.execute(sql, params).fetchone()
    return (f"기간 {start_date}~{end_date} / category={category or '전체'} / region={region or '전체'}\n"
            f"주문수={r['orders']:,}  매출={r['revenue'] or 0:,}원  판매수량={r['units'] or 0:,}")


@mcp.tool()
def top_products(limit: int = 5, start_date: str = "2026-01-01",
                 end_date: str = "2026-12-31") -> str:
    """기간 내 매출 상위 상품 N개를 반환합니다."""
    sql = ("SELECT p.name, o.category, SUM(o.revenue) revenue, SUM(o.quantity) units "
           "FROM orders o JOIN products p ON o.product_id=p.product_id "
           "WHERE o.order_date BETWEEN ? AND ? "
           "GROUP BY o.product_id ORDER BY revenue DESC LIMIT ?")
    with _conn() as con:
        rows = con.execute(sql, [start_date, end_date, limit]).fetchall()
    lines = [f"{i+1}. {r['name']} ({r['category']}) - {r['revenue']:,}원, {r['units']:,}개"
             for i, r in enumerate(rows)]
    return "매출 상위 상품\n" + "\n".join(lines)


@mcp.tool()
def run_sql(query: str) -> str:
    """임의의 SELECT 쿼리를 실행합니다 (read-only). 테이블: orders, products."""
    q = query.strip().rstrip(";")
    if not q.lower().startswith("select"):
        return "오류: SELECT 쿼리만 허용됩니다."
    try:
        with _conn() as con:
            rows = con.execute(q).fetchall()
    except sqlite3.Error as e:
        return f"쿼리 오류: {e}"
    if not rows:
        return "(결과 없음)"
    header = " | ".join(rows[0].keys())
    body = "\n".join(" | ".join(str(v) for v in dict(r).values()) for r in rows[:50])
    return f"{header}\n{body}"


if __name__ == "__main__":
    mcp.run()
