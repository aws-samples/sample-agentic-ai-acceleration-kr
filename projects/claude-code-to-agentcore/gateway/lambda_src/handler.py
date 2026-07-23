"""
handler.py - AgentCore Gateway의 Lambda 타깃.

Part 1의 로컬 stdio MCP 서버(`local_agent/mcp_server/ecommerce_mcp.py`)에 있던
도구 3개(query_sales / top_products / run_sql)를 **로직 변경 없이** 그대로
옮겨온 것입니다. 달라진 건 "진입점"뿐입니다:

  • 로컬 MCP : FastMCP가 stdin/stdout으로 JSON-RPC를 받음
  • Gateway  : Gateway가 도구 호출을 Lambda 이벤트로 변환해 넘김.
               어떤 도구인지는 context.client_context.custom 의
               'bedrockAgentCoreToolName' 에 담겨 온다 (target___tool 형식).

ecommerce.db 는 이 디렉토리에 함께 패키징되어 /var/task 에서 읽힌다 (read-only).
"""
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecommerce.db")


def _conn():
    # /var/task 는 read-only 이지만, 도구 차원에서도 읽기 전용으로 연다.
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ── 도구 구현 (로컬 MCP와 동일한 로직) ─────────────────────────────────────
def query_sales(start_date="2026-01-01", end_date="2026-12-31", category="", region="", **_):
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


def top_products(limit=5, start_date="2026-01-01", end_date="2026-12-31", **_):
    sql = ("SELECT p.name, o.category, SUM(o.revenue) revenue, SUM(o.quantity) units "
           "FROM orders o JOIN products p ON o.product_id=p.product_id "
           "WHERE o.order_date BETWEEN ? AND ? "
           "GROUP BY o.product_id ORDER BY revenue DESC LIMIT ?")
    with _conn() as con:
        rows = con.execute(sql, [start_date, end_date, int(limit)]).fetchall()
    lines = [f"{i+1}. {r['name']} ({r['category']}) - {r['revenue']:,}원, {r['units']:,}개"
             for i, r in enumerate(rows)]
    return "매출 상위 상품\n" + "\n".join(lines)


def run_sql(query="", **_):
    q = (query or "").strip().rstrip(";")
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


TOOLS = {"query_sales": query_sales, "top_products": top_products, "run_sql": run_sql}


def handler(event, context):
    # Gateway는 호출할 도구 이름을 client_context.custom 에 넣어준다.
    tool = ""
    try:
        tool = context.client_context.custom["bedrockAgentCoreToolName"]
    except Exception:
        tool = event.get("__tool__", "")
    # 'ecommerce___query_sales' 처럼 target 프리픽스가 붙어 오므로 분리
    if "___" in tool:
        tool = tool.split("___", 1)[1]
    fn = TOOLS.get(tool)
    if not fn:
        return {"statusCode": 400, "body": f"알 수 없는 도구: {tool}"}
    return {"statusCode": 200, "body": fn(**event)}
