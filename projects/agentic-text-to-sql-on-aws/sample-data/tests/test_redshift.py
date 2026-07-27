"""Redshift seed 로직 및 Data API 헬퍼 테스트 (AWS 호출 fake)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sample_data import schema, seed_redshift
from sample_data.redshift_dataapi import (
    RedshiftDataApiClient,
    RedshiftDataApiError,
)


class FakeRedshiftClient:
    """redshift-data 클라이언트 fake.

    describe_statement 는 statement id 별로 미리 지정한 상태 시퀀스를 순차 반환한다.
    get_statement_result 는 id 별로 지정한 결과를 반환한다.
    """

    def __init__(self, *, describe_by_id=None, results_by_id=None):
        self.executed_sqls: list[str] = []
        self.batches: list[list[str]] = []
        self._counter = 0
        self._describe_by_id = describe_by_id or {}
        self._results_by_id = results_by_id or {}
        # 기본: 모든 statement 는 즉시 FINISHED.
        self._default_status = [{"Status": "FINISHED"}]
        # id → 남은 describe 응답 큐.
        self._describe_queues: dict[str, list[dict]] = {}

    def _next_id(self) -> str:
        self._counter += 1
        return f"stmt-{self._counter}"

    def execute_statement(self, *, WorkgroupName, Database, Sql):  # noqa: N803
        self.executed_sqls.append(Sql)
        sid = self._next_id()
        self._describe_queues[sid] = list(
            self._describe_by_id.get("*", self._default_status)
        )
        return {"Id": sid}

    def batch_execute_statement(self, *, WorkgroupName, Database, Sqls):  # noqa: N803
        self.batches.append(list(Sqls))
        sid = self._next_id()
        self._describe_queues[sid] = list(
            self._describe_by_id.get("*", self._default_status)
        )
        return {"Id": sid}

    def describe_statement(self, *, Id):  # noqa: N803
        queue = self._describe_queues.get(Id, list(self._default_status))
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def get_statement_result(self, *, Id):  # noqa: N803
        # 마지막으로 실행된 SQL 기준으로 결과를 고른다(간단화: 큐 방식).
        return self._results_by_id.get(Id, {"Records": []})


def _no_sleep(_seconds: float) -> None:
    pass


# --- redshift_dataapi 폴링 --------------------------------------------------


def test_execute_polls_until_finished():
    client = FakeRedshiftClient(
        describe_by_id={"*": [{"Status": "STARTED"}, {"Status": "FINISHED"}]}
    )
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    desc = api.execute("SELECT 1")
    assert desc["Status"] == "FINISHED"
    assert client.executed_sqls == ["SELECT 1"]


def test_execute_raises_on_failed():
    client = FakeRedshiftClient(
        describe_by_id={"*": [{"Status": "FAILED", "Error": "syntax error"}]}
    )
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    with pytest.raises(RedshiftDataApiError) as exc:
        api.execute("BAD SQL")
    assert "syntax error" in str(exc.value)


def test_execute_raises_on_aborted():
    client = FakeRedshiftClient(describe_by_id={"*": [{"Status": "ABORTED"}]})
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    with pytest.raises(RedshiftDataApiError):
        api.execute("SELECT 1")


def test_execute_timeout():
    # 항상 STARTED 만 반환 → 타임아웃.
    client = FakeRedshiftClient(describe_by_id={"*": [{"Status": "STARTED"}]})
    api = RedshiftDataApiClient(
        client, "wg", "ecommerce", poll_interval=0.5, timeout=1.0, sleep=_no_sleep
    )
    with pytest.raises(RedshiftDataApiError) as exc:
        api.execute("SELECT 1")
    assert "타임아웃" in str(exc.value)


def test_batch_uses_batch_execute_statement():
    client = FakeRedshiftClient()
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    api.batch(["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"])
    assert client.batches == [["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]]


def test_query_returns_statement_result():
    client = FakeRedshiftClient()
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    # execute_statement 가 stmt-1 을 발급하므로 그 id 에 결과를 심는다.
    client._results_by_id["stmt-1"] = {"Records": [[{"longValue": 42}]]}
    result = api.query("SELECT COUNT(*) FROM orders")
    assert result["Records"] == [[{"longValue": 42}]]


def test_workgroup_only_no_secret_arn():
    # workgroup 이름만으로 호출(SecretArn 미지정 → 관리자 권한).
    client = FakeRedshiftClient()
    api = RedshiftDataApiClient(client, "my-wg", "ecommerce", sleep=_no_sleep)
    api.execute("SELECT 1")
    # FakeRedshiftClient.execute_statement 시그니처가 SecretArn 을 받지 않으므로
    # 호출이 성공한 사실 자체가 SecretArn 미전달을 증명한다.
    assert client.executed_sqls == ["SELECT 1"]


# --- 값 이스케이프 / INSERT 생성 --------------------------------------------


def test_sql_literal_escapes_and_types():
    assert seed_redshift.sql_literal(None) == "NULL"
    assert seed_redshift.sql_literal(True) == "TRUE"
    assert seed_redshift.sql_literal(False) == "FALSE"
    assert seed_redshift.sql_literal(5) == "5"
    assert seed_redshift.sql_literal(Decimal("2.50")) == "2.50"
    assert seed_redshift.sql_literal("O'Brien") == "'O''Brien'"
    ts = dt.datetime(2026, 1, 1, 12, 0, 0)
    assert seed_redshift.sql_literal(ts) == "'2026-01-01 12:00:00'::timestamp"


def test_format_value_timestamp_column():
    col = schema.CUSTOMERS.columns[4]  # created_at TIMESTAMP
    assert col.type.upper().startswith("TIMESTAMP")
    out = seed_redshift.format_value(col, "2026-01-01 00:00:00")
    assert out == "'2026-01-01 00:00:00'::timestamp"


def test_format_value_numeric_column_unquoted():
    price_col = next(c for c in schema.PRODUCTS.columns if c.name == "price")
    out = seed_redshift.format_value(price_col, "199.00")
    assert out == "199.00"  # 인용 없음


def test_format_value_string_column_escaped():
    name_col = next(c for c in schema.CUSTOMERS.columns if c.name == "name")
    assert seed_redshift.format_value(name_col, "김'철수") == "'김''철수'"


def test_format_value_none_is_null():
    col = next(c for c in schema.CUSTOMERS.columns if c.name == "last_login_at")
    assert seed_redshift.format_value(col, None) == "NULL"


def test_build_insert_sql_multirow():
    rows = [
        {"id": 1, "name": "전자제품", "description": "설명1"},
        {"id": 2, "name": "의류", "description": None},
    ]
    sql = seed_redshift.build_insert_sql(schema.CATEGORIES, rows)
    assert sql.startswith("INSERT INTO categories (id, name, description) VALUES ")
    assert "(1, '전자제품', '설명1')" in sql
    assert "(2, '의류', NULL)" in sql
    # 두 튜플이 콤마로 연결.
    assert sql.count("(") >= 2


# --- 스키마 DDL (인덱스 제외) -----------------------------------------------


def test_iter_schema_ddl_excludes_indexes():
    statements = seed_redshift.iter_schema_ddl()
    joined = "\n".join(statements)
    assert "CREATE INDEX" not in joined
    assert "CREATE TABLE IF NOT EXISTS customers" in joined
    assert "COMMENT ON TABLE customers" in joined


def test_apply_schema_uses_batch():
    client = FakeRedshiftClient()
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    seed_redshift.apply_schema(api)
    assert len(client.batches) == 1
    assert all("CREATE INDEX" not in s for s in client.batches[0])


# --- 멱등 skip / 재적재 분기 ------------------------------------------------


def test_load_data_skips_when_counts_match():
    dataset = _tiny_dataset()
    expected = dataset.row_counts()

    class CountingClient(RedshiftDataApiClient):
        pass

    # 각 테이블 COUNT(*) 가 기대치와 일치하도록 결과를 심는다.
    client = _client_with_counts(expected)
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    seed_redshift.load_data(api, dataset)
    # INSERT / TRUNCATE 가 없어야 함.
    assert not any("INSERT INTO" in s for s in client.executed_sqls)
    assert not any("TRUNCATE" in s for s in client.executed_sqls)


def test_load_data_truncates_and_reloads_on_mismatch():
    dataset = _tiny_dataset()
    # 모든 테이블에 기존 행 수를 999(불일치)로 심는다.
    counts = {name: 999 for name in dataset.row_counts()}
    client = _client_with_counts(counts)
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    seed_redshift.load_data(api, dataset)
    assert any("TRUNCATE TABLE customers" in s for s in client.executed_sqls)
    assert any("INSERT INTO customers" in s for s in client.executed_sqls)


def test_load_data_inserts_when_table_empty():
    dataset = _tiny_dataset()
    counts = {name: 0 for name in dataset.row_counts()}
    client = _client_with_counts(counts)
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    seed_redshift.load_data(api, dataset)
    # 빈 테이블은 TRUNCATE 없이 바로 INSERT.
    assert not any("TRUNCATE" in s for s in client.executed_sqls)
    assert any("INSERT INTO categories" in s for s in client.executed_sqls)


# --- read-only 사용자 ------------------------------------------------------


def test_create_readonly_user_creates_when_absent():
    client = FakeRedshiftClient()  # pg_user 조회 결과 비어있음 → 미존재
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    seed_redshift.create_readonly_user(api, "agent_ro", "pw123")
    assert any("CREATE USER agent_ro" in s for s in client.executed_sqls)
    grant_sqls = " ".join(s for b in client.batches for s in b)
    assert "GRANT SELECT ON ALL TABLES" in grant_sqls
    assert "ALTER DEFAULT PRIVILEGES" in grant_sqls
    # 쓰기 권한 없음.
    assert "GRANT INSERT" not in grant_sqls
    assert "GRANT ALL" not in grant_sqls


def test_create_readonly_user_alters_when_exists():
    # pg_user 조회가 행을 반환하도록.
    client = FakeRedshiftClient()
    api = RedshiftDataApiClient(client, "wg", "ecommerce", sleep=_no_sleep)
    # query 는 execute_statement stmt-1 을 쓰므로 그 결과에 행을 심는다.
    client._results_by_id["stmt-1"] = {"Records": [[{"longValue": 1}]]}
    seed_redshift.create_readonly_user(api, "agent_ro", "newpw")
    assert any("ALTER USER agent_ro PASSWORD 'newpw'" in s for s in client.executed_sqls)
    assert not any("CREATE USER" in s for s in client.executed_sqls)


def test_read_agent_ro_password():
    class Secrets:
        def get_secret_value(self, *, SecretId):  # noqa: N803
            assert SecretId == "arn:secret:agent_ro"
            return {"SecretString": '{"username": "agent_ro", "password": "s3cret"}'}

    username, pw = seed_redshift.read_agent_ro_password(Secrets(), "arn:secret:agent_ro")
    assert username == "agent_ro"
    assert pw == "s3cret"


# --- helpers ----------------------------------------------------------------


def _tiny_dataset():
    return seed_redshift.generator.generate(n_customers=2, n_products=2, n_orders=2)


def _client_with_counts(counts: dict[str, int]) -> FakeRedshiftClient:
    """COUNT(*) 쿼리에 대해 테이블별 지정 행 수를 반환하는 fake.

    load_data 는 테이블별로 query(COUNT) 를 먼저 호출한 뒤 execute(INSERT/TRUNCATE) 한다.
    테이블 순서(schema.TABLES)를 따라 COUNT 응답을 순차 매핑한다.
    """
    # COUNT(*) SQL 을 파싱해 테이블별 지정 행 수를 돌려주는 fake 로 위임.
    order = [t.name for t in schema.TABLES]
    return _CountAwareClient(counts, order)


class _CountAwareClient(FakeRedshiftClient):
    """COUNT(*) SQL 을 파싱해 테이블별 지정 행 수를 반환하는 fake."""

    def __init__(self, counts: dict[str, int], order: list[str]):
        super().__init__()
        self._counts = counts
        self._last_count_id: dict[str, str] = {}

    def execute_statement(self, *, WorkgroupName, Database, Sql):  # noqa: N803
        resp = super().execute_statement(
            WorkgroupName=WorkgroupName, Database=Database, Sql=Sql
        )
        if Sql.upper().startswith("SELECT COUNT(*)"):
            # 테이블명 추출: "SELECT COUNT(*) FROM <table>"
            table = Sql.rsplit("FROM", 1)[1].strip()
            self._results_by_id[resp["Id"]] = {
                "Records": [[{"longValue": self._counts.get(table, -1)}]]
            }
        return resp
