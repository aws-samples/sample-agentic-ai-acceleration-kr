"""FastMCP 엔트리포인트 — sql-execution-mcp.

AgentCore Runtime MCP 호스팅 규격(2026-07 검증): Host 0.0.0.0, 포트 8000, POST /mcp,
stateless streamable-HTTP. ``FastMCP(host="0.0.0.0", stateless_http=True)`` +
``mcp.run(transport="streamable-http")``.

도구: run_sql(sql, datasource) — datasource별 (검증 파이프라인, 실행기) 레지스트리로
라우팅한다. SQLGlot allow-list 검증 → Data API 실행 → 정규화 결과 반환. 거부 시 감사 로그 기록.
- aurora: 운영 e-커머스 DB(RDS Data API, 기본값).
- redshift: 분석 웨어하우스(Redshift Data API, 동일 스키마). env 미설정 시 graceful error.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from sql_execution_mcp import audit
from sql_execution_mcp.executor import (
    AuroraDataApiExecutor,
    BaseExecutor,
    RedshiftDataApiExecutor,
)
from sql_execution_mcp.validation import SqlValidationPipeline

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("sql_execution_mcp.server")

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

DEFAULT_DATASOURCE = "aurora"

# 검증 파이프라인은 상태가 없어 datasource별로 모듈 레벨에서 미리 생성(dialect만 다르다).
_pipelines: dict[str, SqlValidationPipeline] = {
    "aurora": SqlValidationPipeline(dialect="postgres"),
    "redshift": SqlValidationPipeline(dialect="redshift"),
}

# 실행기는 lazy(첫 호출 시 boto3 클라이언트 생성). datasource별 싱글턴 캐시.
_executors: dict[str, BaseExecutor] = {}


def _build_executor(datasource: str) -> BaseExecutor:
    """datasource별 실행기 생성. env 미설정 등 구성 오류는 KeyError로 전파된다."""
    if datasource == "aurora":
        return AuroraDataApiExecutor()
    if datasource == "redshift":
        return RedshiftDataApiExecutor()
    raise KeyError(datasource)


def _get_executor(datasource: str) -> BaseExecutor:
    executor = _executors.get(datasource)
    if executor is None:
        executor = _build_executor(datasource)
        _executors[datasource] = executor
    return executor


def _redshift_configured() -> bool:
    """Redshift 실행에 필요한 env가 모두 설정됐는지 확인."""
    return all(
        os.environ.get(name)
        for name in ("REDSHIFT_WORKGROUP", "REDSHIFT_DB", "REDSHIFT_SECRET_ARN")
    )


@mcp.tool()
def run_sql(sql: str, datasource: str = DEFAULT_DATASOURCE) -> dict[str, Any]:
    """자연어에서 생성된 SQL을 안전하게 검증·실행한다.

    SELECT/WITH(CTE)만 허용하는 SQLGlot AST allow-list를 통과한 read-only 쿼리만
    선택된 데이터소스에서 실행한다(read-only 자격증명).

    Args:
        sql: 실행할 단일 SQL 문.
        datasource: 실행 대상 데이터소스. 기본 "aurora".
            - "aurora": 운영 e-커머스 DB(Aurora PostgreSQL, 기본값).
            - "redshift": 분석 웨어하우스(Redshift Serverless, 동일 스키마).
              대량 집계·이력 분석에 적합.

    Returns:
        성공: {"status":"ok","columns":[...],"rows":[[...]],"row_count":N,"truncated":bool}
        거부: {"status":"rejected","reason":"...","rule":"..."}
        실행오류: {"status":"error","message":"..."}  (self-correction 루프가 message 사용)
    """
    # 알 수 없는 datasource → rejected(검증 이전에 라우팅 단계에서 차단).
    if datasource not in _pipelines:
        reason = (
            f"알 수 없는 datasource입니다: {datasource!r}. "
            "허용: 'aurora', 'redshift'."
        )
        audit.log_rejected(sql, rule="unknown_datasource", reason=reason, datasource=datasource)
        return {"status": "rejected", "reason": reason, "rule": "unknown_datasource"}

    # Redshift env 미설정 시 graceful error(크래시 대신 self-correction 가능한 메시지).
    if datasource == "redshift" and not _redshift_configured():
        return {
            "status": "error",
            "message": "Redshift 미구성: REDSHIFT_WORKGROUP/REDSHIFT_DB/REDSHIFT_SECRET_ARN 환경 변수가 필요합니다.",  # noqa: E501
        }

    result = _pipelines[datasource].validate(sql)
    if not result.ok:
        # 거부 쿼리 전수 감사 로깅(§4.5 안전장치 4).
        audit.log_rejected(
            sql,
            rule=result.rule or "unknown",
            reason=result.reason or "",
            datasource=datasource,
        )
        return {
            "status": "rejected",
            "reason": result.reason,
            "rule": result.rule,
        }

    validated_sql = result.sql
    assert validated_sql is not None

    try:
        execution = _get_executor(datasource).execute(validated_sql)
    except Exception as exc:  # noqa: BLE001 — 실행 오류를 정규화해 self-correction 루프에 전달
        # 예외 메시지에 시크릿이 섞이지 않도록 타입+메시지만 전달(자격증명 값 로깅 금지).
        message = f"{type(exc).__name__}: {exc}"
        logger.warning("sql_execution_error[%s]: %s", datasource, message)
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
