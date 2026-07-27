"""DatasourceRegistry·embedding·repository_factory 유닛 테스트."""

from __future__ import annotations

import json

import pytest

from datasource_admin_mcp import repository_factory
from datasource_admin_mcp.embedding import EmbeddingClient, make_embedder
from datasource_admin_mcp.registry import (
    BUILTIN_DATASOURCES,
    VALID_ENGINES,
    DatasourceRegistry,
    RegistryError,
    build_builtin_connector,
    sanitize_config,
    secret_name,
)

from .fakes import FakeBedrockClient, FakeSecretsClient

# --- 시크릿 이름 규칙 ---------------------------------------------------------


def test_secret_name_uses_contract_prefix(monkeypatch) -> None:
    monkeypatch.delenv("DATASOURCE_SECRET_PREFIX", raising=False)
    assert secret_name("warehouse") == "agentic-t2sql/datasource/warehouse"


def test_secret_name_honors_env_and_normalizes_slash(monkeypatch) -> None:
    monkeypatch.setenv("DATASOURCE_SECRET_PREFIX", "custom/ds")  # 후행 / 없음
    assert secret_name("wh") == "custom/ds/wh"


# --- 자격증명 제거 ------------------------------------------------------------


def test_sanitize_config_strips_all_credential_keys() -> None:
    config = {
        "host": "h",
        "database": "d",
        "username": "u",
        "password": "p",
        "Secret": "s",
        "token": "t",
        "private_key": "k",
        "credentials": {"a": 1},
    }
    assert sanitize_config(config) == {"host": "h", "database": "d", "username": "u"}


# --- 등록 저장소 --------------------------------------------------------------


def _registry() -> tuple[DatasourceRegistry, FakeSecretsClient]:
    client = FakeSecretsClient()
    return DatasourceRegistry(prefix="agentic-t2sql/datasource/", client=client), client


def test_store_config_creates_then_updates() -> None:
    registry, client = _registry()
    arn1 = registry.store_config("wh", {"host": "h"})
    arn2 = registry.store_config("wh", {"host": "h2"})
    assert arn1 == arn2
    assert json.loads(client.store["agentic-t2sql/datasource/wh"])["host"] == "h2"


def test_store_config_reraises_non_exists_errors() -> None:
    registry, client = _registry()

    def _boom(**kwargs):
        raise PermissionError("denied")

    client.create_secret = _boom
    with pytest.raises(PermissionError):
        registry.store_config("wh", {"host": "h"})


def test_describe_missing_raises_registry_error() -> None:
    registry, _ = _registry()
    with pytest.raises(RegistryError, match="등록되지 않은 데이터소스"):
        registry.describe("ghost")


def test_validate_secret_requires_all_keys() -> None:
    registry, client = _registry()
    client.store["agentic-t2sql/datasource/wh"] = json.dumps({"host": "h", "database": "d"})
    with pytest.raises(RegistryError, match="필수 키 누락"):
        registry.validate_secret("wh")


def test_validate_secret_rejects_non_object_json() -> None:
    registry, client = _registry()
    client.store["agentic-t2sql/datasource/wh"] = json.dumps(["not", "a", "dict"])
    with pytest.raises(RegistryError, match="JSON 객체가 아닙니다"):
        registry.validate_secret("wh")


def test_validate_secret_detail_lists_keys_not_values() -> None:
    registry, client = _registry()
    client.store["agentic-t2sql/datasource/wh"] = json.dumps(
        {"host": "myhost", "database": "d", "username": "u", "password": "TOPSECRET"}
    )
    detail = registry.validate_secret("wh")
    assert "TOPSECRET" not in detail
    assert "myhost" not in detail
    assert "password" in detail  # 키 이름만 노출


# --- 커넥터 팩토리 ------------------------------------------------------------


def test_build_builtin_connector_rejects_custom_id() -> None:
    with pytest.raises(RegistryError, match="내장 데이터소스가 아닙니다"):
        build_builtin_connector("warehouse")


def test_contract_constants() -> None:
    # register_datasource 시그니처의 engine 유효값 / 내장 소스 목록.
    assert VALID_ENGINES == ("aurora-postgresql", "redshift-serverless")
    assert BUILTIN_DATASOURCES == ("aurora", "redshift")


def test_build_builtin_connector_aurora_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AURORA_CLUSTER_ARN", "arn:cluster")
    monkeypatch.setenv("AURORA_SECRET_ARN", "arn:secret")
    monkeypatch.setenv("DB_NAME", "ecommerce")
    connector = build_builtin_connector("aurora")
    assert connector.name == "aurora"
    assert connector.cluster_arn == "arn:cluster"


def test_build_builtin_connector_redshift_from_env(monkeypatch) -> None:
    monkeypatch.setenv("REDSHIFT_WORKGROUP", "wg")
    monkeypatch.setenv("REDSHIFT_DB", "analytics")
    monkeypatch.setenv("REDSHIFT_SECRET_ARN", "arn:rs")
    connector = build_builtin_connector("redshift")
    assert connector.name == "redshift"
    assert connector.secret_arn == "arn:rs"


# --- 임베딩 -------------------------------------------------------------------


def test_embedding_client_calls_titan_with_normalize() -> None:
    bedrock = FakeBedrockClient()
    client = EmbeddingClient(model_id="amazon.titan-embed-text-v2:0", client=bedrock)
    vector = client.embed("최근 활성 고객")
    assert len(vector) == 1024
    assert bedrock.calls[0]["dimensions"] == 1024
    assert bedrock.calls[0]["normalize"] is True
    assert bedrock.calls[0]["inputText"] == "최근 활성 고객"


def test_make_embedder_returns_callable() -> None:
    embedder = make_embedder(client=FakeBedrockClient())
    assert len(embedder("매출")) == 1024


# --- repository_factory -------------------------------------------------------


def test_get_repository_requires_table_name(monkeypatch) -> None:
    monkeypatch.delenv("SEMANTIC_TABLE_NAME", raising=False)
    repository_factory.reset()
    with pytest.raises(RuntimeError, match="SEMANTIC_TABLE_NAME"):
        repository_factory.get_repository()


def test_get_repository_is_singleton(monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_TABLE_NAME", "agentic-t2sql-semantic")
    repository_factory.reset()
    try:
        first = repository_factory.get_repository()
        assert repository_factory.get_repository() is first
        # embedder 가 주입돼 term/fewshot 쓰기가 가능해야 한다.
        assert first._embedder is not None
    finally:
        repository_factory.reset()
