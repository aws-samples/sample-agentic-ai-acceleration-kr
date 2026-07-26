"""RDS Data API 실행기 (read-only agent_ro 자격증명).

EX 평가는 gold SQL 과 생성 SQL 을 **같은 조건**으로 실행해 결과셋을 비교한다.
Aurora PostgreSQL Data API(동기 ExecuteStatement)만 사용한다(§9.1 — goldset 은 aurora 만).

boto3 클라이언트는 지연 생성하며 주입 가능하다(단위 테스트는 fake 주입).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

DEFAULT_REGION = "us-west-2"
# 비교 대상 결과 상한. 초과분은 잘라내지 않고 오류로 취급(잘라내면 오탐 위험).
DEFAULT_MAX_ROWS = 5000


class DataApiError(RuntimeError):
    """Data API 실행 실패."""


@dataclass
class QueryResult:
    """실행 결과."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)


class AuroraReadOnlyRunner:
    """rds-data ExecuteStatement 로 SELECT 를 실행하는 최소 러너."""

    def __init__(
        self,
        cluster_arn: str | None = None,
        secret_arn: str | None = None,
        db_name: str | None = None,
        region: str | None = None,
        client: Any | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self.cluster_arn = cluster_arn or os.environ.get("AURORA_CLUSTER_ARN", "")
        self.secret_arn = secret_arn or os.environ.get("AURORA_SECRET_ARN", "")
        self.db_name = db_name or os.environ.get("DB_NAME", "")
        self.region = region or os.environ.get("AWS_REGION", DEFAULT_REGION)
        self.max_rows = max_rows
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("rds-data", region_name=self.region)
        return self._client

    def require_config(self) -> None:
        """필수 env(ARN·DB명) 누락을 조기에 검증한다."""
        missing = [
            name
            for name, value in (
                ("AURORA_CLUSTER_ARN", self.cluster_arn),
                ("AURORA_SECRET_ARN", self.secret_arn),
                ("DB_NAME", self.db_name),
            )
            if not value
        ]
        if missing:
            raise DataApiError(f"필수 환경 변수 누락: {', '.join(missing)}")

    def run(self, sql: str) -> QueryResult:
        """SELECT 를 실행하고 columns/rows 로 정규화. 실패는 DataApiError."""
        self.require_config()
        try:
            response = self.client.execute_statement(
                resourceArn=self.cluster_arn,
                secretArn=self.secret_arn,
                database=self.db_name,
                sql=sql,
                includeResultMetadata=True,
                continueAfterTimeout=False,
                formatRecordsAs="NONE",
            )
        except DataApiError:
            raise
        except Exception as exc:  # noqa: BLE001 — 모든 실패를 평가 오류로 정규화
            raise DataApiError(f"Data API 실행 실패: {exc}") from exc
        result = _normalize(response)
        if result.row_count > self.max_rows:
            raise DataApiError(
                f"결과 행 수({result.row_count})가 비교 상한({self.max_rows})을 초과했습니다."
            )
        return result


def _normalize(response: dict[str, Any]) -> QueryResult:
    metadata = response.get("columnMetadata") or []
    columns = [str(col.get("label") or col.get("name") or "") for col in metadata]
    records = response.get("records") or []
    rows = [[_field_value(f) for f in record] for record in records]
    return QueryResult(columns=columns, rows=rows)


def _field_value(field_dict: Any) -> Any:
    """Data API field dict 를 파이썬 스칼라로 변환 (executor.py 와 동일 규약)."""
    if not isinstance(field_dict, dict):
        return field_dict
    if field_dict.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in field_dict:
            value = field_dict[key]
            if key == "blobValue" and isinstance(value, (bytes, bytearray)):
                return value.decode("utf-8", errors="replace")
            return value
    if "arrayValue" in field_dict:
        return _array_value(field_dict["arrayValue"])
    return None


def _array_value(array_field: Any) -> Any:
    if not isinstance(array_field, dict):
        return []
    for key in ("stringValues", "longValues", "doubleValues", "booleanValues"):
        if key in array_field:
            return list(array_field[key])
    if "arrayValues" in array_field:
        return [_array_value(inner) for inner in array_field["arrayValues"]]
    return []
