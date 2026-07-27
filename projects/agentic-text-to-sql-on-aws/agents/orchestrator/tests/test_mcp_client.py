import pytest

from orchestrator.mcp_client import MCP_SERVICE_NAME, build_runtime_mcp_url


def test_build_url_encodes_arn():
    arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/my-mcp-abc"
    url = build_runtime_mcp_url(arn, "us-west-2")
    assert url.startswith("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/")
    # ARN 의 콜론/슬래시가 URL 인코딩됨
    assert "%3A" in url
    assert "%2F" in url
    assert url.endswith("/invocations?qualifier=DEFAULT")


def test_build_url_custom_qualifier():
    url = build_runtime_mcp_url("arn:x", "us-east-1", qualifier="PROD")
    assert "qualifier=PROD" in url
    assert "us-east-1" in url


def test_build_url_empty_arn_raises():
    with pytest.raises(ValueError, match="runtime_arn"):
        build_runtime_mcp_url("", "us-west-2")


def test_service_name_is_bedrock_agentcore():
    assert MCP_SERVICE_NAME == "bedrock-agentcore"
