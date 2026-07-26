"""거부 쿼리 감사 로깅 — stdout에 structured JSON(§4.5 안전장치 4).

CloudWatch가 stdout을 수집한다. SQL 원문은 로그에 남기지 않고 sha256 해시만 남긴다
(민감 정보·시크릿 노출 방지). 타임스탬프는 UTC ISO-8601.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import UTC, datetime

_logger = logging.getLogger("sql_execution_mcp.audit")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def sql_hash(sql: str) -> str:
    """SQL 원문의 sha256(원문 로깅 금지 — 해시로 상관관계만 추적)."""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def log_rejected(sql: str, rule: str, reason: str, datasource: str = "aurora") -> None:
    """거부된 쿼리를 structured JSON 한 줄로 기록.

    ``datasource`` 는 M3에서 추가된 필드(기본 aurora — 기존 호출 하위호환).
    """
    record = {
        "event": "sql_rejected",
        "rule": rule,
        "reason": reason,
        "datasource": datasource,
        "sql_hash": sql_hash(sql),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    _logger.info(json.dumps(record, ensure_ascii=False))
