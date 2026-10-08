"""
Tests that a gateway-backed MCP record composes into a harness correctly.

An AgentCore gateway speaks streamable-HTTP MCP like any other server, but it
demands SigV4 that a `remote_mcp` tool cannot supply — attaching it that way
yields a harness whose tools all fail at call time with 403. These cover the
choice of tool type, and that the endpoint-URL requirement is relaxed for
gateway records (which bind by ARN instead).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.harness import ComposeHarnessRequest  # noqa: E402
from models.registry import (  # noqa: E402
    CreateRecordRequest,
    DESCRIPTOR_MCP,
    RegistryRecordDetail,
)
from services.harness_service import HarnessService  # noqa: E402
from services.registry_service import _build_descriptors, gateway_arn_of  # noqa: E402

GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/builtin-tools-abc123"
GATEWAY_URL = "https://builtin-tools-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def mcp_record(record_id, name, content):
    return RegistryRecordDetail(
        record_id=record_id,
        name=name,
        descriptor_type=DESCRIPTOR_MCP,
        status="APPROVED",
        descriptor_content=content,
    )


def gateway_record():
    return mcp_record(
        "rec-gw",
        "bap/builtin_tools",
        {
            "server": {
                "name": "bap/builtin_tools",
                "remotes": [{"type": "streamable-http", "url": GATEWAY_URL}],
                "gatewayArn": GATEWAY_ARN,
            }
        },
    )


def plain_record():
    return mcp_record(
        "rec-exa",
        "bap/exa-search",
        {
            "server": {
                "name": "bap/exa-search",
                "remotes": [{"type": "streamable-http", "url": "https://mcp.exa.ai/mcp"}],
            }
        },
    )


class StubRegistry:
    def __init__(self, records):
        self._records = {r.record_id: r for r in records}

    def get_record(self, record_id):
        return self._records[record_id]


class StubControl:
    def __init__(self):
        self.params = None

    def create_harness(self, **params):
        self.params = params
        return {
            "harness": {
                "harnessId": "h-1",
                "arn": "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1",
                "harnessName": params["harnessName"],
                "status": "CREATING",
            }
        }


def build(records):
    service = HarnessService(
        registry=StubRegistry(records),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )
    control = StubControl()
    service._control = control
    return service, control


def tools_for(records, **request_kwargs):
    service, control = build(records)
    service.create_harness(
        ComposeHarnessRequest(
            name="test_agent",
            mcp_record_ids=[r.record_id for r in records],
            **request_kwargs,
        )
    )
    return control.params.get("tools", [])


def test_gateway_record_attaches_as_a_native_gateway_tool():
    """`remote_mcp` cannot sign SigV4, so a gateway must not be attached that way."""
    tools = tools_for([gateway_record()])

    assert len(tools) == 1
    assert tools[0]["type"] == "agentcore_gateway"
    config = tools[0]["config"]["agentCoreGateway"]
    assert config["gatewayArn"] == GATEWAY_ARN
    assert config["outboundAuth"] == {"awsIam": {}}


def test_plain_mcp_record_still_attaches_as_remote_mcp():
    tools = tools_for([plain_record()])

    assert len(tools) == 1
    assert tools[0]["type"] == "remote_mcp"
    assert tools[0]["config"]["remoteMcp"]["url"] == "https://mcp.exa.ai/mcp"


def test_gateway_and_plain_records_compose_together():
    tools = tools_for([gateway_record(), plain_record()])

    assert [t["type"] for t in tools] == ["agentcore_gateway", "remote_mcp"]


def test_a_gateway_named_twice_is_attached_once():
    """
    Selecting the record and pasting the same ARN must not attach two tools: the
    harness rejects duplicate tool names, so this would fail the whole compose.
    """
    tools = tools_for([gateway_record()], gateway_arns=[GATEWAY_ARN])

    assert len(tools) == 1
    assert tools[0]["config"]["agentCoreGateway"]["gatewayArn"] == GATEWAY_ARN


def test_a_different_gateway_arn_is_still_attached():
    other = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/other-gw-xyz789"
    tools = tools_for([gateway_record()], gateway_arns=[other])

    arns = [t["config"]["agentCoreGateway"]["gatewayArn"] for t in tools]
    assert arns == [GATEWAY_ARN, other]


def test_gateway_record_without_a_url_is_still_composable():
    """A gateway binds by ARN, so the endpoint URL is not what makes it usable."""
    record = mcp_record(
        "rec-gw",
        "bap/builtin_tools",
        {"server": {"name": "bap/builtin_tools", "gatewayArn": GATEWAY_ARN}},
    )
    tools = tools_for([record])

    assert tools[0]["type"] == "agentcore_gateway"


def test_plain_record_without_a_url_is_rejected():
    record = mcp_record("rec-bad", "bap/no-url", {"server": {"name": "x"}})
    service, _ = build([record])

    with pytest.raises(ValueError, match="no endpoint URL"):
        service.create_harness(
            ComposeHarnessRequest(name="test_agent", mcp_record_ids=["rec-bad"])
        )


def test_gateway_arn_survives_a_descriptor_round_trip():
    """
    The ARN is carried in the MCP server descriptor, which has no field for it.
    AWS preserves unknown keys verbatim, and this is what depends on that.
    """
    descriptors = _build_descriptors(
        CreateRecordRequest(
            name="builtin_tools",
            descriptor_type=DESCRIPTOR_MCP,
            version="1.0.0",
            remote_url=GATEWAY_URL,
            gateway_arn=GATEWAY_ARN,
        )
    )
    server = json.loads(descriptors["mcpServer"]["data"])

    assert gateway_arn_of({"server": server}) == GATEWAY_ARN
    # The endpoint is recorded too, so the record is readable without the gateway API.
    assert server["remotes"][0]["url"] == GATEWAY_URL


def test_a_plain_mcp_record_has_no_gateway_arn():
    descriptors = _build_descriptors(
        CreateRecordRequest(
            name="exa-search",
            descriptor_type=DESCRIPTOR_MCP,
            remote_url="https://mcp.exa.ai/mcp",
        )
    )
    server = json.loads(descriptors["mcpServer"]["data"])

    assert "gatewayArn" not in server
    assert gateway_arn_of({"server": server}) is None


def test_gateway_arn_of_ignores_malformed_values():
    assert gateway_arn_of(None) is None
    assert gateway_arn_of({"server": "not-a-dict"}) is None
    assert gateway_arn_of({"server": {"gatewayArn": "builtin-tools"}}) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
