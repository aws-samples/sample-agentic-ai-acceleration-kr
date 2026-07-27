"""데이터소스 커넥터 — 연결 점검(SELECT 1)과 information_schema 크롤.

추상 base ``DatasourceConnector`` + 두 구현체(레포 원칙: 복잡한 모듈은 OOP):

- ``AuroraDataApiConnector``  — boto3 rds-data ``ExecuteStatement``(동기). Aurora PostgreSQL.
- ``RedshiftDataApiConnector`` — boto3 redshift-data(비동기: execute → polling → get_result).

Data API 호출 패턴은 ``sql-execution-mcp/executor.py`` 와 동형이다(패키지 간 의존을 만들지
않기 위해 의도적으로 복사 — 두 런타임 이미지는 서로 독립이다).

주의: Redshift Data API 는 **SecretArn 을 반드시 넘겨야** 한다.
생략하면 IAM 매핑 사용자로 실행돼 information_schema 조회조차 권한 부족으로 실패한다.

이 커넥터는 관리 평면(크롤·점검) 전용이며 read-only 자격증명만 사용한다.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# 크롤 대상 스키마(데모 스키마는 public 하나).
DEFAULT_SCHEMA = "public"

# Redshift Data API 폴링 파라미터.
REDSHIFT_POLL_INTERVAL_SEC = 0.5
REDSHIFT_TIMEOUT_SEC = 60.0
_REDSHIFT_TERMINAL = frozenset({"FINISHED", "FAILED", "ABORTED"})


class ConnectorError(RuntimeError):
    """커넥터 내부 오류(폴링 타임아웃·원격 실패 등). server 가 error 응답으로 정규화한다."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]


@dataclass
class CrawledSchema:
    """크롤 결과 — semantic 엔티티로 변환 가능한 중립 표현."""

    tables: list[dict[str, Any]] = field(default_factory=list)
    columns: list[dict[str, Any]] = field(default_factory=list)
    joins: list[dict[str, Any]] = field(default_factory=list)


# --- information_schema 질의 (PostgreSQL 계열 공통) ---------------------------
# Redshift 는 PostgreSQL 8.0.2 기반이라 아래 세 질의를 동일하게 지원한다.

_TABLES_SQL = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE'
ORDER BY table_name
"""

_COLUMNS_SQL = """
SELECT table_name, column_name, data_type, is_nullable, character_maximum_length
FROM information_schema.columns
WHERE table_schema = '{schema}'
ORDER BY table_name, ordinal_position
"""

# 외래키(join 후보). Redshift 는 FK 를 강제하지 않지만 제약 메타데이터는 보존한다.
_FOREIGN_KEYS_SQL = """
SELECT tc.table_name, kcu.column_name, ccu.table_name AS ref_table, ccu.column_name AS ref_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = '{schema}'
ORDER BY tc.table_name, kcu.column_name
"""


class DatasourceConnector(ABC):
    """데이터소스 커넥터 추상 base class.

    구현체는 ``run_query`` 만 구현하면 되고, 연결 점검·크롤 로직은 base 가 공유한다.
    """

    #: 커넥터 식별자(datasource_id).
    name: str = "unknown"

    def __init__(self, schema: str = DEFAULT_SCHEMA) -> None:
        self.schema = schema

    @abstractmethod
    def run_query(self, sql: str) -> QueryResult:
        """SQL 을 실행하고 columns/rows 로 정규화한다(read-only 자격증명)."""
        raise NotImplementedError

    # --- 공통 관리 동작 -----------------------------------------------------
    def test_connection(self) -> str:
        """``SELECT 1`` 로 연결을 점검하고 사람이 읽을 detail 문자열을 반환."""
        result = self.run_query("SELECT 1")
        first = result.rows[0][0] if result.rows and result.rows[0] else None
        return f"{self.name}: SELECT 1 성공 (결과={first!r})"

    def crawl(self) -> CrawledSchema:
        """information_schema 를 크롤해 table/column/join 정의를 수집한다.

        FK 질의는 엔진에 따라 실패할 수 있어(권한·미지원) 실패 시 joins 만 비운다.
        """
        crawled = CrawledSchema()

        tables = self.run_query(_TABLES_SQL.format(schema=self.schema))
        table_names = [str(row[0]) for row in tables.rows if row and row[0] is not None]

        columns = self.run_query(_COLUMNS_SQL.format(schema=self.schema))
        columns_by_table: dict[str, list[dict[str, Any]]] = {}
        for row in columns.rows:
            record = _as_record(columns.columns, row)
            table = str(record.get("table_name") or "")
            if not table:
                continue
            columns_by_table.setdefault(table, []).append(record)

        try:
            fks = self.run_query(_FOREIGN_KEYS_SQL.format(schema=self.schema))
            fk_records = [_as_record(fks.columns, row) for row in fks.rows]
        except Exception as exc:  # noqa: BLE001 — FK 미지원/권한 부족은 크롤 전체를 막지 않는다
            self._log_fk_failure(exc)
            fk_records = []

        # column → 참조 대상 매핑(references 페이로드 + join 엔티티 생성에 함께 사용).
        references: dict[tuple[str, str], tuple[str, str]] = {}
        for record in fk_records:
            table = str(record.get("table_name") or "")
            column = str(record.get("column_name") or "")
            ref_table = str(record.get("ref_table") or "")
            ref_column = str(record.get("ref_column") or "")
            if not (table and column and ref_table and ref_column):
                continue
            references[(table, column)] = (ref_table, ref_column)

        for table in table_names:
            table_columns = columns_by_table.get(table, [])
            crawled.tables.append(
                {
                    "entity_type": "table",
                    "entity_id": table,
                    "payload": {
                        "table": table,
                        "description": f"{self.name} {self.schema}.{table} (자동 크롤)",
                        "ddl_snippet": _build_ddl(table, table_columns, references),
                        "source_datasource": self.name,
                    },
                }
            )
            for record in table_columns:
                column = str(record.get("column_name") or "")
                if not column:
                    continue
                reference = references.get((table, column))
                crawled.columns.append(
                    {
                        "entity_type": "column",
                        "entity_id": f"{table}.{column}",
                        "payload": {
                            "table": table,
                            "column": column,
                            "data_type": _type_text(record),
                            "description": f"{table}.{column} (자동 크롤)",
                            "ddl_snippet": _column_ddl(record, reference),
                            "references": f"{reference[0]}({reference[1]})" if reference else None,
                            "source_datasource": self.name,
                        },
                    }
                )
                if reference:
                    ref_table, ref_column = reference
                    crawled.joins.append(
                        {
                            "entity_type": "join",
                            "entity_id": f"{table}.{column}->{ref_table}.{ref_column}",
                            "payload": {
                                "left_table": table,
                                "right_table": ref_table,
                                "join_on": f"{table}.{column} = {ref_table}.{ref_column}",
                                "source_datasource": self.name,
                            },
                        }
                    )

        return crawled

    def _log_fk_failure(self, exc: Exception) -> None:
        """FK 질의 실패 로깅(하위 클래스/테스트에서 관찰 가능하도록 분리)."""
        import logging

        logging.getLogger("datasource_admin_mcp.connectors").warning(
            "foreign_key_crawl_failed[%s]: %s: %s", self.name, type(exc).__name__, exc
        )


class AuroraDataApiConnector(DatasourceConnector):
    """RDS Data API(동기) 기반 Aurora PostgreSQL 커넥터."""

    name = "aurora"

    def __init__(
        self,
        cluster_arn: str | None = None,
        secret_arn: str | None = None,
        db_name: str | None = None,
        region: str | None = None,
        schema: str = DEFAULT_SCHEMA,
        client: Any | None = None,
    ) -> None:
        super().__init__(schema=schema)
        self.cluster_arn = cluster_arn or os.environ["AURORA_CLUSTER_ARN"]
        self.secret_arn = secret_arn or os.environ["AURORA_SECRET_ARN"]
        self.db_name = db_name or os.environ["DB_NAME"]
        self.region = region or os.environ.get("AWS_REGION", "us-west-2")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("rds-data", region_name=self.region)
        return self._client

    def run_query(self, sql: str) -> QueryResult:
        response = self.client.execute_statement(
            resourceArn=self.cluster_arn,
            secretArn=self.secret_arn,
            database=self.db_name,
            sql=sql,
            includeResultMetadata=True,
            # 타임아웃 시 롤백(장시간 실행 방어). 절대 continue 하지 않는다.
            continueAfterTimeout=False,
            formatRecordsAs="NONE",
        )
        metadata = response.get("columnMetadata", [])
        columns = [col.get("label") or col.get("name") for col in metadata]
        rows = [
            [_field_value(field_value) for field_value in record]
            for record in response.get("records", [])
        ]
        return QueryResult(columns=columns, rows=rows)


class RedshiftDataApiConnector(DatasourceConnector):
    """Redshift Data API(비동기 폴링) 기반 Redshift Serverless 커넥터.

    ``SecretArn`` 필수 — 생략 시 IAM 매핑 사용자로 실행돼 권한 부족으로 실패한다.
    ``sleep`` 은 주입 가능(테스트에서 실제 대기 제거).
    """

    name = "redshift"

    def __init__(
        self,
        workgroup: str | None = None,
        db_name: str | None = None,
        secret_arn: str | None = None,
        region: str | None = None,
        schema: str = DEFAULT_SCHEMA,
        client: Any | None = None,
        poll_interval: float = REDSHIFT_POLL_INTERVAL_SEC,
        timeout: float = REDSHIFT_TIMEOUT_SEC,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(schema=schema)
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
            import boto3

            self._client = boto3.client("redshift-data", region_name=self.region)
        return self._client

    def run_query(self, sql: str) -> QueryResult:
        submit = self.client.execute_statement(
            WorkgroupName=self.workgroup,
            Database=self.db_name,
            # 필수 — 없으면 IAM 매핑 사용자로 실행된다(배포 실측).
            SecretArn=self.secret_arn,
            Sql=sql,
        )
        statement_id = submit["Id"]
        self._wait_until_done(statement_id)
        result = self.client.get_statement_result(Id=statement_id)
        metadata = result.get("ColumnMetadata", [])
        columns = [col.get("label") or col.get("name") for col in metadata]
        rows = [
            [_redshift_field_value(field_value) for field_value in record]
            for record in result.get("Records", [])
        ]
        return QueryResult(columns=columns, rows=rows)

    def _wait_until_done(self, statement_id: str) -> None:
        """describe_statement 를 종료 상태까지 폴링. 실패·타임아웃은 ConnectorError."""
        elapsed = 0.0
        while True:
            description = self.client.describe_statement(Id=statement_id)
            status = description.get("Status")
            if status == "FINISHED":
                return
            if status in _REDSHIFT_TERMINAL:  # FAILED / ABORTED
                error = description.get("Error") or f"쿼리가 {status} 상태로 종료되었습니다."
                raise ConnectorError(f"Redshift 쿼리 실패({status}): {error}")

            if elapsed >= self.timeout:
                try:
                    self.client.cancel_statement(Id=statement_id)
                except Exception:  # noqa: BLE001 — 취소 실패는 무시하고 타임아웃으로 정규화
                    pass
                raise ConnectorError(
                    f"Redshift 쿼리 타임아웃({self.timeout:.0f}s 초과). 쿼리를 취소했습니다."
                )

            self._sleep(self.poll_interval)
            elapsed += self.poll_interval


# --- 헬퍼 -------------------------------------------------------------------


def _as_record(columns: list[str], row: list[Any]) -> dict[str, Any]:
    """columns/row → 컬럼명 키 dict."""
    return {str(name): value for name, value in zip(columns, row, strict=False)}


def _type_text(record: dict[str, Any]) -> str:
    """information_schema.columns 레코드 → 사람이 읽는 타입 표기."""
    data_type = str(record.get("data_type") or "unknown")
    length = record.get("character_maximum_length")
    if length:
        return f"{data_type}({int(length)})"
    return data_type


def _column_ddl(record: dict[str, Any], reference: tuple[str, str] | None) -> str:
    """컬럼 하나의 DDL 조각(seed_semantic 의 column.ddl() 과 동형 표기)."""
    parts = [str(record.get("column_name") or ""), _type_text(record)]
    if str(record.get("is_nullable") or "YES").upper() == "NO":
        parts.append("NOT NULL")
    if reference:
        parts.append(f"REFERENCES {reference[0]}({reference[1]})")
    return " ".join(part for part in parts if part)


def _build_ddl(
    table: str,
    columns: list[dict[str, Any]],
    references: dict[tuple[str, str], tuple[str, str]],
) -> str:
    """크롤 결과로 CREATE TABLE 스니펫 재구성(검색 컨텍스트 품질용)."""
    lines = [
        f"  {_column_ddl(record, references.get((table, str(record.get('column_name') or ''))))}"
        for record in columns
    ]
    body = ",\n".join(lines)
    return f"CREATE TABLE {table} (\n{body}\n);"


def _field_value(field_value: dict[str, Any]) -> Any:
    """RDS Data API field → 파이썬 스칼라."""
    if field_value.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in field_value:
            value = field_value[key]
            if key == "blobValue" and isinstance(value, (bytes, bytearray)):
                return value.decode("utf-8", errors="replace")
            return value
    return None


def _redshift_field_value(field_value: dict[str, Any]) -> Any:
    """Redshift Data API field → 파이썬 스칼라."""
    if field_value.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in field_value:
            return field_value[key]
    return None
