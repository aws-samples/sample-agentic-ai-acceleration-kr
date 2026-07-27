"""데이터소스 실행기 — read-only 자격증명으로 검증된 SQL만 실행.

추상 base class ``BaseExecutor`` + 두 구현체:
- ``AuroraDataApiExecutor`` — boto3 rds-data ExecuteStatement(동기). Aurora PostgreSQL.
  (하위호환 alias ``SqlExecutor = AuroraDataApiExecutor``.)
- ``RedshiftDataApiExecutor`` — boto3 redshift-data(비동기: execute→polling→get_result).
  Redshift Serverless 분석 웨어하우스.

공통 원칙:
- 커넥션 풀 불필요(Data API), IAM + Secrets Manager 기반, read-only(agent_ro) 시크릿이 최후 방어선.
- 결과 행 수 상한 초과 시 잘라내고 truncated=True (동일 계약).
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3

# 결과 행 수 상한. 이 값을 넘으면 잘라내고 truncated=True.
DEFAULT_MAX_ROWS = 500

# Redshift Data API 폴링 파라미터.
REDSHIFT_POLL_INTERVAL_SEC = 0.5
REDSHIFT_TIMEOUT_SEC = 60.0
# describe_statement 의 종료 상태(성공/실패).
_REDSHIFT_TERMINAL = frozenset({"FINISHED", "FAILED", "ABORTED"})


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


class ExecutorError(RuntimeError):
    """실행기 내부 오류(폴링 타임아웃·원격 실패 등). server가 error 응답으로 정규화한다."""


class BaseExecutor(ABC):
    """데이터소스 실행기 추상 base class.

    구현체는 검증을 통과한 SQL을 실행하고 :class:`ExecutionResult`로 정규화한다.
    ``max_rows`` truncation 계약은 모든 구현체가 동일하게 지킨다.
    """

    def __init__(self, max_rows: int = DEFAULT_MAX_ROWS) -> None:
        self.max_rows = max_rows

    @abstractmethod
    def execute(self, sql: str) -> ExecutionResult:
        """검증을 통과한 SQL을 실행하고 columns/rows로 정규화한다."""
        raise NotImplementedError

    def _truncate(
        self, columns: list[str], all_rows: list[list[Any]]
    ) -> ExecutionResult:
        """정규화된 전체 행에 max_rows 상한을 적용(모든 실행기 공통 계약)."""
        truncated = len(all_rows) > self.max_rows
        rows = all_rows[: self.max_rows] if truncated else all_rows
        return ExecutionResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )


class AuroraDataApiExecutor(BaseExecutor):
    """RDS Data API 기반 SQL 실행기(read-only, Aurora PostgreSQL).

    ExecuteStatement는 동기 호출이라 폴링이 불필요하다.
    - continueAfterTimeout=False 강제(장시간 쓰기성 쿼리 방어).
    """

    def __init__(
        self,
        cluster_arn: str | None = None,
        secret_arn: str | None = None,
        db_name: str | None = None,
        region: str | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        client: Any | None = None,
    ) -> None:
        super().__init__(max_rows=max_rows)
        self.cluster_arn = cluster_arn or os.environ["AURORA_CLUSTER_ARN"]
        self.secret_arn = secret_arn or os.environ["AURORA_SECRET_ARN"]
        self.db_name = db_name or os.environ["DB_NAME"]
        self.region = region or os.environ.get("AWS_REGION", "us-west-2")
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
        rows = [[_field_value(field) for field in record] for record in records]
        return self._truncate(columns, rows)


# 하위호환 alias — 기존 코드/테스트가 ``SqlExecutor``로 임포트한다.
SqlExecutor = AuroraDataApiExecutor


class RedshiftDataApiExecutor(BaseExecutor):
    """Redshift Data API 기반 SQL 실행기(read-only, Redshift Serverless).

    Redshift Data API는 **비동기**다:
    1. ``execute_statement`` → statement Id 반환(즉시).
    2. ``describe_statement`` 폴링 — Status 가 FINISHED/FAILED/ABORTED가 될 때까지.
    3. FINISHED면 ``get_statement_result`` 로 결과(ColumnMetadata + Records) 수집.

    타임아웃(기본 60s) 초과 시 ``cancel_statement`` 를 시도하고 :class:`ExecutorError`.
    ``sleep`` 은 주입 가능(테스트에서 실제 대기 제거).
    """

    def __init__(
        self,
        workgroup: str | None = None,
        db_name: str | None = None,
        secret_arn: str | None = None,
        region: str | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        client: Any | None = None,
        poll_interval: float = REDSHIFT_POLL_INTERVAL_SEC,
        timeout: float = REDSHIFT_TIMEOUT_SEC,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(max_rows=max_rows)
        self.workgroup = workgroup or os.environ["REDSHIFT_WORKGROUP"]
        self.db_name = db_name or os.environ["REDSHIFT_DB"]
        self.secret_arn = secret_arn or os.environ["REDSHIFT_SECRET_ARN"]
        self.region = region or os.environ.get("AWS_REGION", "us-west-2")
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._sleep = sleep
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("redshift-data", region_name=self.region)
        return self._client

    def execute(self, sql: str) -> ExecutionResult:
        """검증을 통과한 SQL을 비동기 실행하고 폴링 후 columns/rows로 정규화한다."""
        submit = self.client.execute_statement(
            WorkgroupName=self.workgroup,
            Database=self.db_name,
            SecretArn=self.secret_arn,
            Sql=sql,
        )
        statement_id = submit["Id"]
        self._wait_until_done(statement_id)
        result = self.client.get_statement_result(Id=statement_id)
        return self._normalize(result)

    def _wait_until_done(self, statement_id: str) -> None:
        """describe_statement 를 종료 상태까지 폴링. 실패·타임아웃은 ExecutorError."""
        elapsed = 0.0
        while True:
            description = self.client.describe_statement(Id=statement_id)
            status = description.get("Status")
            if status == "FINISHED":
                return
            if status in _REDSHIFT_TERMINAL:  # FAILED / ABORTED
                error = description.get("Error") or f"쿼리가 {status} 상태로 종료되었습니다."
                raise ExecutorError(f"Redshift 쿼리 실패({status}): {error}")

            if elapsed >= self.timeout:
                # 타임아웃 — 실행 중인 statement 취소를 시도(best-effort)하고 오류 반환.
                try:
                    self.client.cancel_statement(Id=statement_id)
                except Exception:  # noqa: BLE001 — 취소 실패는 무시하고 타임아웃으로 정규화
                    pass
                raise ExecutorError(
                    f"Redshift 쿼리 타임아웃({self.timeout:.0f}s 초과). 쿼리를 취소했습니다."
                )

            self._sleep(self.poll_interval)
            elapsed += self.poll_interval

    def _normalize(self, result: dict[str, Any]) -> ExecutionResult:
        metadata = result.get("ColumnMetadata", [])
        columns = [col.get("label") or col.get("name") for col in metadata]
        records = result.get("Records", [])
        rows = [[_redshift_field_value(field) for field in record] for record in records]
        return self._truncate(columns, rows)


def _redshift_field_value(field: dict[str, Any]) -> Any:
    """Redshift Data API field 딕셔너리를 파이썬 스칼라로 변환.

    각 필드는 {stringValue|longValue|doubleValue|booleanValue|isNull} 중 하나.
    (blobValue는 base64 문자열로 온다.)
    """
    if field.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in field:
            return field[key]
    return None


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
