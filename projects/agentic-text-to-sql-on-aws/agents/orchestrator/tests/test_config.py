import pytest

from orchestrator.config import DEFAULT_MODEL_ID, DEFAULT_REGION, Settings


def test_from_env_defaults_when_empty():
    s = Settings.from_env({})
    assert s.model_id == DEFAULT_MODEL_ID
    assert s.region == DEFAULT_REGION
    assert s.max_sql_corrections == 3
    assert s.mode == "graph"
    assert s.memory_id is None


def test_from_env_reads_values():
    s = Settings.from_env(
        {
            "SQL_MCP_ARN": "arn:sql",
            "SEMANTIC_MCP_ARN": "arn:sem",
            "MEMORY_ID": "mem-1",
            "MODEL_ID": "custom-model",
            "AWS_REGION": "us-east-1",
            "MAX_SQL_CORRECTIONS": "5",
            "ORCHESTRATOR_MODE": "AGENT",
        }
    )
    assert s.sql_mcp_arn == "arn:sql"
    assert s.semantic_mcp_arn == "arn:sem"
    assert s.memory_id == "mem-1"
    assert s.model_id == "custom-model"
    assert s.region == "us-east-1"
    assert s.max_sql_corrections == 5
    assert s.mode == "agent"


def test_max_corrections_invalid_falls_back():
    assert Settings.from_env({"MAX_SQL_CORRECTIONS": "abc"}).max_sql_corrections == 3
    assert Settings.from_env({"MAX_SQL_CORRECTIONS": "-2"}).max_sql_corrections == 3
    assert Settings.from_env({"MAX_SQL_CORRECTIONS": ""}).max_sql_corrections == 3


def test_empty_memory_id_becomes_none():
    assert Settings.from_env({"MEMORY_ID": ""}).memory_id is None


def test_require_mcp_arns_raises_when_missing():
    s = Settings.from_env({"SQL_MCP_ARN": "arn:sql"})
    with pytest.raises(ValueError, match="SEMANTIC_MCP_ARN"):
        s.require_mcp_arns()


def test_require_mcp_arns_ok_when_present():
    s = Settings.from_env({"SQL_MCP_ARN": "arn:sql", "SEMANTIC_MCP_ARN": "arn:sem"})
    s.require_mcp_arns()  # should not raise
