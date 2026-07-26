"""RDS Data API 파라미터 변환 및 seed 로직 테스트 (AWS 호출 mock)."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from sample_data import schema, seed_aurora
from sample_data.dataapi import DataApiClient, row_to_parameters, to_param


def test_to_param_types():
    assert to_param("a", None)["value"] == {"isNull": True}
    assert to_param("a", True)["value"] == {"booleanValue": True}
    assert to_param("a", 5)["value"] == {"longValue": 5}
    assert to_param("a", 1.5)["value"] == {"doubleValue": 1.5}
    assert to_param("a", Decimal("2.5"))["value"] == {"doubleValue": 2.5}
    assert to_param("a", "hi")["value"] == {"stringValue": "hi"}


def test_to_param_bool_before_int():
    # bool 은 int 의 하위타입이므로 booleanValue 로 먼저 처리돼야 함.
    assert "booleanValue" in to_param("a", False)["value"]


def test_to_param_type_hint():
    p = to_param("ts", "2026-01-01 00:00:00", type_hint="TIMESTAMP")
    assert p["typeHint"] == "TIMESTAMP"
    assert p["value"] == {"stringValue": "2026-01-01 00:00:00"}


def test_row_to_parameters_with_hints():
    row = {"id": 1, "price": "10.00"}
    params = row_to_parameters(row, {"price": "DECIMAL"})
    by_name = {p["name"]: p for p in params}
    assert by_name["id"]["value"] == {"longValue": 1}
    assert by_name["price"]["typeHint"] == "DECIMAL"


def test_build_insert_sql_idempotent():
    sql = seed_aurora.build_insert_sql(schema.CUSTOMERS)
    assert sql.startswith("INSERT INTO customers")
    assert "ON CONFLICT (id) DO NOTHING" in sql
    for col in schema.CUSTOMERS.columns:
        assert f":{col.name}" in sql


def test_dataapi_execute_passes_args():
    client = mock.Mock()
    client.execute_statement.return_value = {"records": []}
    api = DataApiClient(client, "cluster-arn", "secret-arn", "ecommerce")
    api.execute("SELECT 1")
    kwargs = client.execute_statement.call_args.kwargs
    assert kwargs["resourceArn"] == "cluster-arn"
    assert kwargs["secretArn"] == "secret-arn"
    assert kwargs["database"] == "ecommerce"
    assert kwargs["sql"] == "SELECT 1"


def test_dataapi_execute_database_override():
    client = mock.Mock()
    client.execute_statement.return_value = {}
    api = DataApiClient(client, "c", "s", "ecommerce")
    api.execute("SELECT 1", database="postgres")
    assert client.execute_statement.call_args.kwargs["database"] == "postgres"


def test_batch_execute_uses_parameter_sets():
    client = mock.Mock()
    client.batch_execute_statement.return_value = {}
    api = DataApiClient(client, "c", "s", "ecommerce")
    api.batch_execute("INSERT ...", [[{"name": "id"}]], transaction_id="tx1")
    kwargs = client.batch_execute_statement.call_args.kwargs
    assert kwargs["parameterSets"] == [[{"name": "id"}]]
    assert kwargs["transactionId"] == "tx1"


def test_ensure_database_skips_when_exists():
    client = mock.Mock()
    client.execute_statement.return_value = {"records": [[{"longValue": 1}]]}
    api = DataApiClient(client, "c", "s", "ecommerce")
    seed_aurora.ensure_database(api, "ecommerce")
    # SELECT 만 호출, CREATE DATABASE 는 없어야 함.
    sqls = [c.kwargs["sql"] for c in client.execute_statement.call_args_list]
    assert not any("CREATE DATABASE" in s for s in sqls)


def test_ensure_database_creates_when_absent():
    client = mock.Mock()
    client.execute_statement.return_value = {"records": []}
    api = DataApiClient(client, "c", "s", "ecommerce")
    seed_aurora.ensure_database(api, "ecommerce")
    sqls = [c.kwargs["sql"] for c in client.execute_statement.call_args_list]
    assert any("CREATE DATABASE" in s for s in sqls)


def test_create_readonly_user_grants_select_only():
    client = mock.Mock()
    client.execute_statement.return_value = {}
    api = DataApiClient(client, "c", "s", "ecommerce")
    seed_aurora.create_readonly_user(api, "pw123")
    sqls = " ".join(c.kwargs["sql"] for c in client.execute_statement.call_args_list)
    assert "GRANT SELECT ON ALL TABLES" in sqls
    assert "ALTER DEFAULT PRIVILEGES" in sqls
    # 쓰기 권한은 부여하지 않음.
    assert "GRANT INSERT" not in sqls
    assert "GRANT UPDATE" not in sqls
    assert "GRANT ALL" not in sqls


def test_ensure_agent_ro_secret_reuses_existing():
    secrets = mock.Mock()
    secrets.get_secret_value.return_value = {
        "SecretString": '{"username": "agent_ro", "password": "existing"}'
    }
    pw = seed_aurora.ensure_agent_ro_secret(
        secrets, "us-west-2", "cluster-arn", "ecommerce", None
    )
    assert pw == "existing"
    secrets.create_secret.assert_not_called()


def test_read_agent_ro_password_from_cdk_secret():
    # CDK 관리 시크릿에서 username/password 를 그대로 읽어야 함(seed 가 새로 만들지 않음).
    secrets = mock.Mock()
    secrets.get_secret_value.return_value = {
        "SecretString": '{"username": "agent_ro", "password": "cdk-managed-pw"}'
    }
    username, pw = seed_aurora.read_agent_ro_password(secrets, "arn:secret:agent_ro")
    assert username == "agent_ro"
    assert pw == "cdk-managed-pw"
    secrets.get_secret_value.assert_called_once_with(SecretId="arn:secret:agent_ro")
    secrets.create_secret.assert_not_called()


def test_seed_uses_cdk_agent_ro_secret_when_arn_given():
    # agent_ro_secret_arn 이 주어지면 self-generate 하지 않고 그 시크릿 비밀번호로 역할 동기화.
    rds = mock.Mock()
    rds.execute_statement.return_value = {"records": [[{"longValue": 1}]]}
    rds.batch_execute_statement.return_value = {}
    rds.begin_transaction.return_value = {"transactionId": "tx"}
    rds.commit_transaction.return_value = {}
    secrets = mock.Mock()
    secrets.get_secret_value.return_value = {
        "SecretString": '{"username": "agent_ro", "password": "cdk-managed-pw"}'
    }
    seed_aurora.seed(
        cluster_arn="c",
        secret_arn="master-secret",
        db_name="ecommerce",
        region="us-west-2",
        agent_ro_secret_arn="arn:secret:agent_ro",
        n_customers=1,
        n_products=1,
        n_orders=1,
        rds_data_client=rds,
        secrets_client=secrets,
    )
    # CDK 시크릿을 읽었고, 별도 시크릿 생성은 하지 않아야 함.
    secrets.get_secret_value.assert_called_with(SecretId="arn:secret:agent_ro")
    secrets.create_secret.assert_not_called()
    # DB 역할 비밀번호가 CDK 시크릿 값으로 설정됐는지 확인.
    role_sqls = " ".join(
        c.kwargs.get("sql", "") for c in rds.execute_statement.call_args_list
    )
    assert "cdk-managed-pw" in role_sqls


def test_ensure_agent_ro_secret_creates_when_missing():
    secrets = mock.Mock()

    class RNFE(Exception):
        pass

    secrets.exceptions.ResourceNotFoundException = RNFE
    secrets.get_secret_value.side_effect = RNFE()
    secrets.get_random_password.return_value = {"RandomPassword": "generated-pw"}
    pw = seed_aurora.ensure_agent_ro_secret(
        secrets, "us-west-2", "cluster-arn", "ecommerce", "host.example.com"
    )
    assert pw == "generated-pw"
    secrets.create_secret.assert_called_once()
    created = secrets.create_secret.call_args.kwargs
    assert created["Name"] == seed_aurora.AGENT_RO_SECRET_NAME
