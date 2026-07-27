"""데이터소스 도구 유닛 테스트 (register/test/crawl). fake boto3 주입, AWS 호출 없음."""

from __future__ import annotations

import json

import pytest

from datasource_admin_mcp import repository_factory, server
from datasource_admin_mcp.registry import DatasourceRegistry

from .fakes import FakeRepository, FakeSecretsClient


@pytest.fixture()
def wired() -> tuple[FakeRepository, FakeSecretsClient]:
    repo = FakeRepository()
    secrets = FakeSecretsClient()
    registry = DatasourceRegistry(
        region="us-west-2", prefix="agentic-t2sql/datasource/", client=secrets
    )
    repository_factory.reset(repository=repo, registry=registry)
    yield repo, secrets
    repository_factory.reset()


# --- register_datasource ------------------------------------------------------


def test_register_datasource_stores_secret_and_sanitized_meta(wired) -> None:
    repo, secrets = wired
    result = server.register_datasource(
        "warehouse",
        "redshift-serverless",
        {
            "host": "wh.example.com",
            "database": "analytics",
            "username": "ro",
            "password": "s3cr3t",
        },
        "manager@example.com",
    )
    assert result["status"] == "ok"
    assert result["secret_arn"].startswith("arn:aws:secretsmanager:")

    # 시크릿에는 자격증명 포함.
    stored = json.loads(secrets.store["agentic-t2sql/datasource/warehouse"])
    assert stored["password"] == "s3cr3t"

    # DynamoDB 메타에는 자격증명 제외.
    entity = repo.store[("datasource", "warehouse")]
    assert entity["status"] == "candidate"
    assert entity["engine"] == "redshift-serverless"
    assert entity["host"] == "wh.example.com"
    assert "password" not in entity
    assert entity["updated_by"] == "manager@example.com"
    assert entity["secret_name"] == "agentic-t2sql/datasource/warehouse"


def test_register_datasource_is_idempotent_via_put_secret_value(wired) -> None:
    repo, secrets = wired
    config = {"host": "h", "database": "d", "username": "u", "password": "p1"}
    server.register_datasource("warehouse", "aurora-postgresql", config)
    server.register_datasource("warehouse", "aurora-postgresql", {**config, "password": "p2"})

    assert secrets.calls.count("create_secret") == 2  # 두 번 시도
    assert secrets.calls.count("put_secret_value") == 1  # 두 번째는 폴백
    assert json.loads(secrets.store["agentic-t2sql/datasource/warehouse"])["password"] == "p2"
    assert repo.store[("datasource", "warehouse")]["version"] == 2


def test_register_datasource_rejects_unknown_engine(wired) -> None:
    result = server.register_datasource("x", "mysql", {"host": "h"})
    assert result["status"] == "error"
    assert "지원하지 않는 engine" in result["message"]


def test_register_datasource_rejects_bad_id_and_empty_config(wired) -> None:
    bad_id = server.register_datasource("a/b", "aurora-postgresql", {"host": "h"})
    assert bad_id["status"] == "error"
    assert server.register_datasource("x", "aurora-postgresql", {})["status"] == "error"


def test_register_datasource_error_does_not_leak_secret_value(wired) -> None:
    _, secrets = wired

    def _boom(**kwargs):
        raise RuntimeError("boom")

    secrets.create_secret = _boom
    result = server.register_datasource(
        "warehouse", "aurora-postgresql", {"host": "h", "password": "TOPSECRET"}
    )
    assert result["status"] == "error"
    assert "TOPSECRET" not in result["message"]


# --- test_datasource ----------------------------------------------------------


def test_test_datasource_builtin_runs_select_one(wired, monkeypatch) -> None:
    class FakeConnector:
        name = "aurora"

        def test_connection(self) -> str:
            return "aurora: SELECT 1 성공 (결과=1)"

    monkeypatch.setattr(server, "build_builtin_connector", lambda ds: FakeConnector())
    result = server.test_datasource("aurora")
    assert result == {"status": "ok", "ok": True, "detail": "aurora: SELECT 1 성공 (결과=1)"}


def test_test_datasource_builtin_failure_reports_ok_false(wired, monkeypatch) -> None:
    def _boom(datasource_id: str):
        raise RuntimeError("cluster not available")

    monkeypatch.setattr(server, "build_builtin_connector", _boom)
    result = server.test_datasource("redshift")
    assert result["status"] == "ok"
    assert result["ok"] is False
    assert "cluster not available" in result["detail"]


def test_test_datasource_custom_validates_secret_keys(wired) -> None:
    _, secrets = wired
    secrets.store["agentic-t2sql/datasource/warehouse"] = json.dumps(
        {"host": "h", "database": "d", "username": "u", "password": "p"}
    )
    result = server.test_datasource("warehouse")
    assert result["ok"] is True
    assert "필수 키 검증 통과" in result["detail"]
    # 키 이름만 노출되고 값은 실리지 않는다.
    assert "password" in result["detail"]
    assert "'p'" not in result["detail"]
    assert "'h'" not in result["detail"]


def test_test_datasource_custom_missing_keys(wired) -> None:
    _, secrets = wired
    secrets.store["agentic-t2sql/datasource/warehouse"] = json.dumps({"host": "h"})
    result = server.test_datasource("warehouse")
    assert result["ok"] is False
    assert "필수 키 누락" in result["detail"]
    assert "database" in result["detail"]


def test_test_datasource_unregistered(wired) -> None:
    result = server.test_datasource("ghost")
    assert result == {
        "status": "ok",
        "ok": False,
        "detail": "등록되지 않은 데이터소스입니다: ghost",
    }


# --- crawl_schema -------------------------------------------------------------


def test_crawl_schema_puts_candidate_entities(wired, monkeypatch) -> None:
    repo, _ = wired
    from datasource_admin_mcp.connectors import CrawledSchema, DatasourceConnector

    class FakeConnector(DatasourceConnector):
        name = "aurora"

        def run_query(self, sql: str):  # pragma: no cover - crawl 를 오버라이드
            raise AssertionError

        def crawl(self) -> CrawledSchema:
            return CrawledSchema(
                tables=[{"entity_type": "table", "entity_id": "orders", "payload": {}}],
                columns=[
                    {"entity_type": "column", "entity_id": "orders.id", "payload": {}},
                    {"entity_type": "column", "entity_id": "orders.customer_id", "payload": {}},
                ],
                joins=[
                    {
                        "entity_type": "join",
                        "entity_id": "orders.customer_id->customers.id",
                        "payload": {},
                    }
                ],
            )

    monkeypatch.setattr(server, "build_builtin_connector", lambda ds: FakeConnector())
    result = server.crawl_schema("aurora", "manager@example.com")
    assert result == {"status": "ok", "tables": 1, "columns": 2, "joins": 1}

    # 전부 candidate 로 적재(승인 게이트) + 순서는 table → column → join.
    assert all(put["status"] == "candidate" for put in repo.puts)
    assert [put["entity_type"] for put in repo.puts] == ["table", "column", "column", "join"]
    assert repo.store[("table", "orders")]["updated_by"] == "manager@example.com"


def test_crawl_schema_rejects_custom_datasource(wired) -> None:
    result = server.crawl_schema("warehouse")
    assert result["status"] == "error"
    assert "내장 데이터소스만" in result["message"]


def test_crawl_schema_error_is_normalized(wired, monkeypatch) -> None:
    def _boom(datasource_id: str):
        raise RuntimeError("data api down")

    monkeypatch.setattr(server, "build_builtin_connector", _boom)
    result = server.crawl_schema("aurora")
    assert result["status"] == "error"
    assert "RuntimeError: data api down" == result["message"]
