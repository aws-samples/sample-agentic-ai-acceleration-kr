"""커넥터 유닛 테스트 — Data API 호출 형태·크롤 파싱. fake boto3 클라이언트 주입."""

from __future__ import annotations

import pytest

from datasource_admin_mcp.connectors import (
    AuroraDataApiConnector,
    ConnectorError,
    RedshiftDataApiConnector,
)

from .fakes import FakeRdsDataClient, FakeRedshiftDataClient, rds_rows, redshift_rows

_TABLES = [["orders"], ["customers"]]
_COLUMNS = [
    ["orders", "id", "integer", "NO", None],
    ["orders", "customer_id", "integer", "YES", None],
    ["customers", "id", "integer", "NO", None],
    ["customers", "name", "character varying", "YES", 100],
]
_FKS = [["orders", "customer_id", "customers", "id"]]

_COLUMN_LABELS = [
    "table_name",
    "column_name",
    "data_type",
    "is_nullable",
    "character_maximum_length",
]
_FK_LABELS = ["table_name", "column_name", "ref_table", "ref_column"]


def _aurora_connector(**responses) -> tuple[AuroraDataApiConnector, FakeRdsDataClient]:
    client = FakeRdsDataClient(
        {
            "information_schema.tables": rds_rows(["table_name"], _TABLES),
            "information_schema.columns": rds_rows(_COLUMN_LABELS, _COLUMNS),
            "FOREIGN KEY": rds_rows(_FK_LABELS, _FKS),
            "SELECT 1": rds_rows(["?column?"], [[1]]),
            **responses,
        }
    )
    connector = AuroraDataApiConnector(
        cluster_arn="arn:cluster",
        secret_arn="arn:secret",
        db_name="ecommerce",
        region="us-west-2",
        client=client,
    )
    return connector, client


# --- Aurora ------------------------------------------------------------------


def test_aurora_test_connection() -> None:
    connector, client = _aurora_connector()
    detail = connector.test_connection()
    assert "SELECT 1 성공" in detail
    assert client.sqls == ["SELECT 1"]


def test_aurora_execute_statement_uses_safe_params() -> None:
    connector, _ = _aurora_connector()
    captured: dict = {}

    def _execute(**kwargs):
        captured.update(kwargs)
        return rds_rows(["x"], [[1]])

    connector._client.execute_statement = _execute
    connector.run_query("SELECT 1")
    assert captured["resourceArn"] == "arn:cluster"
    assert captured["secretArn"] == "arn:secret"
    assert captured["database"] == "ecommerce"
    assert captured["continueAfterTimeout"] is False


def test_aurora_crawl_builds_table_column_join_entities() -> None:
    connector, _ = _aurora_connector()
    crawled = connector.crawl()

    assert [t["entity_id"] for t in crawled.tables] == ["orders", "customers"]
    assert [c["entity_id"] for c in crawled.columns] == [
        "orders.id",
        "orders.customer_id",
        "customers.id",
        "customers.name",
    ]
    assert [j["entity_id"] for j in crawled.joins] == ["orders.customer_id->customers.id"]

    join = crawled.joins[0]["payload"]
    assert join["left_table"] == "orders"
    assert join["right_table"] == "customers"
    assert join["join_on"] == "orders.customer_id = customers.id"

    # column payload: 타입/nullable/FK 표기 + seed_semantic 과 동형 필드.
    fk_column = next(c for c in crawled.columns if c["entity_id"] == "orders.customer_id")
    assert fk_column["payload"]["references"] == "customers(id)"
    assert fk_column["payload"]["table"] == "orders"
    assert fk_column["payload"]["column"] == "customer_id"

    varchar = next(c for c in crawled.columns if c["entity_id"] == "customers.name")
    assert varchar["payload"]["data_type"] == "character varying(100)"

    pk = next(c for c in crawled.columns if c["entity_id"] == "orders.id")
    assert "NOT NULL" in pk["payload"]["ddl_snippet"]

    # table ddl_snippet 은 CREATE TABLE 스니펫으로 재구성된다.
    assert crawled.tables[0]["payload"]["ddl_snippet"].startswith("CREATE TABLE orders (")
    assert "REFERENCES customers(id)" in crawled.tables[0]["payload"]["ddl_snippet"]


def test_crawl_tolerates_foreign_key_query_failure() -> None:
    connector, _ = _aurora_connector(**{"FOREIGN KEY": RuntimeError("permission denied")})
    crawled = connector.crawl()
    # FK 실패 시 joins 만 비고 table/column 은 정상 수집(graceful degrade).
    assert crawled.joins == []
    assert len(crawled.tables) == 2
    assert len(crawled.columns) == 4
    assert crawled.columns[1]["payload"]["references"] is None


# --- Redshift ----------------------------------------------------------------


def _redshift_connector(status: str = "FINISHED"):
    client = FakeRedshiftDataClient(
        {
            "information_schema.tables": redshift_rows(["table_name"], _TABLES),
            "information_schema.columns": redshift_rows(_COLUMN_LABELS, _COLUMNS),
            "FOREIGN KEY": redshift_rows(_FK_LABELS, _FKS),
            "SELECT 1": redshift_rows(["?column?"], [[1]]),
        },
        status=status,
    )
    connector = RedshiftDataApiConnector(
        workgroup="agentic-t2sql-rs-wg",
        db_name="analytics",
        secret_arn="arn:rs-secret",
        region="us-west-2",
        client=client,
        poll_interval=0.0,
        timeout=0.0,
        sleep=lambda _: None,
    )
    return connector, client


def test_redshift_always_passes_secret_arn() -> None:
    # M3 학습: SecretArn 없으면 IAM 매핑 사용자로 실행돼 권한 부족.
    connector, client = _redshift_connector()
    connector.test_connection()
    assert client.kwargs[0]["SecretArn"] == "arn:rs-secret"
    assert client.kwargs[0]["WorkgroupName"] == "agentic-t2sql-rs-wg"
    assert client.kwargs[0]["Database"] == "analytics"


def test_redshift_crawl_parses_records() -> None:
    connector, _ = _redshift_connector()
    crawled = connector.crawl()
    assert [t["entity_id"] for t in crawled.tables] == ["orders", "customers"]
    assert len(crawled.columns) == 4
    assert crawled.joins[0]["payload"]["join_on"] == "orders.customer_id = customers.id"
    assert crawled.tables[0]["payload"]["source_datasource"] == "redshift"


def test_redshift_failed_statement_raises_connector_error() -> None:
    connector, _ = _redshift_connector(status="FAILED")
    with pytest.raises(ConnectorError, match="Redshift 쿼리 실패"):
        connector.run_query("SELECT 1")


def test_redshift_timeout_cancels_statement() -> None:
    connector, client = _redshift_connector(status="STARTED")
    with pytest.raises(ConnectorError, match="타임아웃"):
        connector.run_query("SELECT 1")
    assert client.cancelled == ["stmt-1"]


def test_redshift_null_and_typed_values() -> None:
    connector, client = _redshift_connector()
    client.responses["SELECT vals"] = redshift_rows(["a", "b", "c"], [[None, 1.5, True]])
    result = connector.run_query("SELECT vals")
    assert result.rows == [[None, 1.5, True]]
