"""Boundary translation between the internal model and the agent-registry schema."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.registry_service import (  # noqa: E402
    _record_type_of,
    _descriptor_type_of,
    _build_descriptors,
    _extract_descriptor_content,
    _as_updated_descriptors,
)
from models.registry import (  # noqa: E402
    CreateRecordRequest,
    DESCRIPTOR_A2A,
    DESCRIPTOR_MCP,
    DESCRIPTOR_AGENT_SKILLS,
    DESCRIPTOR_CUSTOM,
)


def test_record_type_mapping_round_trips_by_descriptor_key():
    assert _record_type_of(DESCRIPTOR_A2A) == "AGENT"
    assert _record_type_of(DESCRIPTOR_MCP) == "MCP"
    assert _record_type_of(DESCRIPTOR_AGENT_SKILLS) == "SKILL"
    assert _record_type_of(DESCRIPTOR_CUSTOM) == "CUSTOM"
    assert _descriptor_type_of({"a2aAgentCard": {}}) == DESCRIPTOR_A2A
    assert _descriptor_type_of({"mcpServer": {}}) == DESCRIPTOR_MCP
    assert _descriptor_type_of({"agentSkillsDefinition": {}}) == DESCRIPTOR_AGENT_SKILLS
    assert _descriptor_type_of({"custom": {}}) == DESCRIPTOR_CUSTOM


def test_build_a2a_uses_new_flat_key_and_data_field():
    req = CreateRecordRequest(
        name="planner", descriptor_type=DESCRIPTOR_A2A,
        harness_arn="arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1",
    )
    d = _build_descriptors(req)
    assert set(d) == {"a2aAgentCard"}
    card = json.loads(d["a2aAgentCard"]["data"])
    assert card["harnessArn"].endswith("harness/h-1")
    assert d["a2aAgentCard"]["dataSchemaVersion"]
    assert "inlineContent" not in d["a2aAgentCard"]


def test_build_mcp_puts_server_under_mcpserver_data():
    req = CreateRecordRequest(
        name="tools", descriptor_type=DESCRIPTOR_MCP,
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:1:gateway/g-1",
    )
    d = _build_descriptors(req)
    assert set(d) == {"mcpServer"}
    server = json.loads(d["mcpServer"]["data"])
    assert server["gatewayArn"].endswith("gateway/g-1")
    assert d["mcpServer"]["dataSchemaVersion"]


def test_build_mcp_clips_the_description_to_the_server_json_limit():
    # server.json 2025-12-11 caps `description` at 100 characters and AWS validates
    # the descriptor against it, so copying a longer record description in verbatim
    # fails the whole CreateRegistryRecord with a ValidationException.
    long_description = "AgentCore built-in tools: web search, code interpreter, browser. " * 3
    req = CreateRecordRequest(
        name="tools", descriptor_type=DESCRIPTOR_MCP, description=long_description,
        remote_url="https://acme.test/mcp",
    )
    server = json.loads(_build_descriptors(req)["mcpServer"]["data"])
    assert len(server["description"]) <= 100
    assert server["description"].startswith(long_description[:60])


def test_build_mcp_keeps_a_short_description_verbatim():
    req = CreateRecordRequest(
        name="tools", descriptor_type=DESCRIPTOR_MCP, description="x" * 100,
        remote_url="https://acme.test/mcp",
    )
    server = json.loads(_build_descriptors(req)["mcpServer"]["data"])
    assert server["description"] == "x" * 100


def test_build_skill_nests_skillmd_under_additionaldata():
    req = CreateRecordRequest(
        name="my-skill", descriptor_type=DESCRIPTOR_AGENT_SKILLS,
        skill_markdown="---\nname: my-skill\n---\n",
    )
    d = _build_descriptors(req)
    assert set(d) == {"agentSkillsDefinition"}
    assert d["agentSkillsDefinition"]["data"]  # skillDefinition JSON
    md = d["agentSkillsDefinition"]["additionalData"]["skillMd"]["data"]
    assert "my-skill" in md


def test_build_custom_uses_data():
    req = CreateRecordRequest(
        name="c", descriptor_type=DESCRIPTOR_CUSTOM, content={"k": "v"},
    )
    d = _build_descriptors(req)
    assert json.loads(d["custom"]["data"]) == {"k": "v"}


def test_extract_reads_new_mcp_shape_into_internal_server_tools():
    descriptors = {
        "mcpServer": {
            "data": json.dumps({"name": "bap/tools", "gatewayArn": "arn:x"}),
            "additionalData": {"tools": {"data": json.dumps([{"name": "t"}])}},
        }
    }
    content = _extract_descriptor_content(descriptors)
    assert content["server"]["gatewayArn"] == "arn:x"
    assert content["tools"] == [{"name": "t"}]


def test_extract_reads_new_skill_shape():
    descriptors = {
        "agentSkillsDefinition": {
            "data": json.dumps({"_meta": {"x": 1}}),
            "additionalData": {"skillMd": {"data": "# md"}},
        }
    }
    content = _extract_descriptor_content(descriptors)
    assert content["skillMd"] == "# md"
    assert content["skillDefinition"] == {"_meta": {"x": 1}}


def test_as_updated_descriptors_wraps_every_independently_settable_field():
    """
    Confirmed against the real `agent-registry-control` service model
    (test_update_descriptor_shape.py): UpdateRegistryRecord's *DescriptorFields
    shapes wrap `data`/`dataSchemaVersion` themselves, not just the union and the
    primary descriptor key — a shallower two-level wrap validates here (no real
    client involved) but the real API rejects it.
    """
    wrapped = _as_updated_descriptors({"a2aAgentCard": {"data": "x", "dataSchemaVersion": "0.3.0"}})
    assert wrapped == {
        "optionalValue": {
            "a2aAgentCard": {
                "optionalValue": {
                    "data": {"optionalValue": "x"},
                    "dataSchemaVersion": {"optionalValue": "0.3.0"},
                }
            }
        }
    }


def test_as_updated_descriptors_wraps_additionaldata_one_level_deeper():
    """MCP's `tools` and AGENT_SKILLS' `skillMd` nest inside `additionalData`,
    each getting its own descriptor-shaped wrap rather than one opaque blob."""
    wrapped = _as_updated_descriptors(
        {
            "agentSkillsDefinition": {
                "data": "{}",
                "additionalData": {"skillMd": {"data": "# md"}},
            }
        }
    )
    assert wrapped == {
        "optionalValue": {
            "agentSkillsDefinition": {
                "optionalValue": {
                    "data": {"optionalValue": "{}"},
                    "additionalData": {
                        "optionalValue": {
                            "skillMd": {
                                "optionalValue": {"data": {"optionalValue": "# md"}}
                            }
                        }
                    },
                }
            }
        }
    }
