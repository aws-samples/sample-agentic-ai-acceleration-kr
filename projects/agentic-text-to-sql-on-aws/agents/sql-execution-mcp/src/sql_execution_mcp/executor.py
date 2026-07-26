"""RDS Data API 실행기 — read-only 자격증명으로 검증된 SQL만 실행.

boto3 rds-data ExecuteStatement 사용. 커넥션 풀 불필요(Data API), IAM + Secrets Manager 기반.
- read-only agent_ro 시크릿(AURORA_SECRET_ARN) — READ-ONLY 4중 방어의 최후 방어선.
- continueAfterTimeout=False 강제(장시간 쓰기성 쿼리 방어).
- 결과 행 수 상한 초과 시 truncated=True.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import boto3

# 결과 행 수 상한. 이 값을 넘으면 잘라내고 truncated=True.
DEFAULT_MAX_ROWS = 500


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


class SqlExecutor:
    """RDS Data API 기반 SQL 실행기(read-only)."""

    def __init__(
        self,
        cluster_arn: str | None = None,
        secret_arn: str | None = None,
        db_name: str | None = None,
        region: str | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        client: Any | None = None,
    ) -> None:
        self.cluster_arn = cluster_arn or os.environ["AURORA_CLUSTER_ARN"]
        self.secret_arn = secret_arn or os.environ["AURORA_SECRET_ARN"]
        self.db_name = db_name or os.environ["DB_NAME"]
        self.region = region or os.environ.get("AWS_REGION", "us-west-2")
        self.max_rows = max_rows
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("rds-data", region_name=self.region)
        return self._client

    def execute(self, sql: str) -> ExecutionResult:
        """검증을 통과한 SQL을 실행하고 columns/rows로 정규화한다."""
        response = self.client.execute_statement(
            resourceArn=self.cluster_arn,
            secretArn=self.secret_arn,
            database=self.db_name,
            sql=sql,
            includeResultMetadata=True,
            # 타임아웃 시 롤백(장시간 실행 방어). 절대 continue 하지 않는다.
            continueAfterTimeout=False,
            # LOB을 인라인으로 받아 후속 조회 왕복을 없앤다.
            formatRecordsAs="NONE",
        )
        return self._normalize(response)

    def _normalize(self, response: dict[str, Any]) -> ExecutionResult:
        metadata = response.get("columnMetadata", [])
        columns = [col.get("label") or col.get("name") for col in metadata]
        records = response.get("records", [])

        total = len(records)
        truncated = total > self.max_rows
        sliced = records[: self.max_rows] if truncated else records

        rows = [[_field_value(field) for field in record] for record in sliced]
        return ExecutionResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )


def _field_value(field: dict[str, Any]) -> Any:
    """Data API field 딕셔너리를 파이썬 스칼라로 변환."""
    if field.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in field:
            value = field[key]
            if key == "blobValue" and isinstance(value, (bytes, bytearray)):
                return value.decode("utf-8", errors="replace")
            return value
    if "arrayValue" in field:
        return _array_value(field["arrayValue"])
    return None


def _array_value(array_field: dict[str, Any]) -> Any:
    """arrayValue(중첩 가능)를 파이썬 리스트로 변환."""
    for key in (
        "stringValues",
        "longValues",
        "doubleValues",
        "booleanValues",
    ):
        if key in array_field:
            return list(array_field[key])
    if "arrayValues" in array_field:
        return [_array_value(inner) for inner in array_field["arrayValues"]]
    return []
