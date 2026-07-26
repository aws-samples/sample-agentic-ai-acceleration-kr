"""FastMCP 엔트리포인트 — sql-execution-mcp.

AgentCore Runtime MCP 호스팅 규격(2026-07 검증): Host 0.0.0.0, 포트 8000, POST /mcp,
stateless streamable-HTTP. ``FastMCP(host="0.0.0.0", stateless_http=True)`` +
``mcp.run(transport="streamable-http")``.

도구: run_sql(sql) — SQLGlot allow-list 검증 → RDS Data API 실행 → 정규화 결과 반환.
거부 시 감사 로그 기록.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from sql_execution_mcp import audit
from sql_execution_mcp.executor import SqlExecutor
from sql_execution_mcp.validation import SqlValidationPipeline

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("sql_execution_mcp.server")

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

# 검증 파이프라인은 상태가 없어 모듈 레벨로 재사용. 실행기는 lazy(첫 호출 시 boto3 클라이언트 생성).
_pipeline = SqlValidationPipeline()
_executor: SqlExecutor | None = None


def _get_executor() -> SqlExecutor:
    global _executor
    if _executor is None:
        _executor = SqlExecutor()
    return _executor


@mcp.tool()
def run_sql(sql: str) -> dict[str, Any]:
    """자연어에서 생성된 SQL을 안전하게 검증·실행한다.

    SELECT/WITH(CTE)만 허용하는 SQLGlot AST allow-list를 통과한 read-only 쿼리만
    Aurora PostgreSQL(RDS Data API, read-only 자격증명)에서 실행한다.

    Args:
        sql: 실행할 단일 SQL 문(PostgreSQL dialect).

    Returns:
        성공: {"status":"ok","columns":[...],"rows":[[...]],"row_count":N,"truncated":bool}
        거부: {"status":"rejected","reason":"...","rule":"..."}
        실행오류: {"status":"error","message":"..."}  (self-correction 루프가 message 사용)
    """
    result = _pipeline.validate(sql)
    if not result.ok:
        # 거부 쿼리 전수 감사 로깅(§4.5 안전장치 4).
        audit.log_rejected(sql, rule=result.rule or "unknown", reason=result.reason or "")
        return {
            "status": "rejected",
            "reason": result.reason,
            "rule": result.rule,
        }

    validated_sql = result.sql
    assert validated_sql is not None

    try:
        execution = _get_executor().execute(validated_sql)
    except Exception as exc:  # noqa: BLE001 — 실행 오류를 정규화해 self-correction 루프에 전달
        # 예외 메시지에 시크릿이 섞이지 않도록 타입+메시지만 전달(자격증명 값 로깅 금지).
        message = f"{type(exc).__name__}: {exc}"
        logger.warning("sql_execution_error: %s", message)
        return {"status": "error", "message": message}

    return {
        "status": "ok",
        "columns": execution.columns,
        "rows": execution.rows,
        "row_count": execution.row_count,
        "truncated": execution.truncated,
    }


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
