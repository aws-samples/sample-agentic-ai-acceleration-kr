"""
Tests for editing a registry record.

UpdateRegistryRecord wraps mutable fields in {"optionalValue": ...} to tell
"clear this" apart from "leave it alone", so what a partial edit sends matters
as much as what it changes. DEPRECATED is terminal and must be refused here
rather than only in the UI.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.registry import RegistryRecordDetail, UpdateRecordRequest  # noqa: E402
from services.registry_service import RegistryService  # noqa: E402


class StubControl:
    def __init__(self):
        self.calls = []

    def update_registry_record(self, **params):
        self.calls.append(params)
        return {
            "recordId": params["recordId"],
            "displayName": "edited",
            "recordType": "AGENT",
            "status": "DRAFT",
        }


def _service(detail):
    service = RegistryService(registry_id="reg-1", region="us-east-1")
    stub = StubControl()
    service._registry_control = stub
    service.get_record = lambda record_id: detail
    return service, stub


RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/agent-abc"


def _detail(status="APPROVED"):
    return RegistryRecordDetail(
        record_id="rec-1",
        name="agent_one",
        description="original description",
        descriptor_type="A2A",
        status=status,
        # Summary-level binding fields, as _to_summary populates them.
        agent_runtime_arn=RUNTIME_ARN,
        descriptor_content={
            "name": "agent_one",
            "description": "original description",
            "version": "2.3",
            "url": RUNTIME_ARN,
            "agentRuntimeArn": RUNTIME_ARN,
        },
    )


def _mcp_detail():
    # _extract_descriptor_content wraps an MCP payload as {"server": ..., "tools": ...},
    # which is NOT the flat shape _build_descriptors reads from `content`.
    return RegistryRecordDetail(
        record_id="rec-2",
        name="acme/tools",
        description="original description",
        descriptor_type="MCP",
        status="APPROVED",
        descriptor_content={
            "server": {
                "name": "acme/tools",
                "description": "original description",
                "version": "1.4.0",
                "remotes": [
                    {"type": "streamable-http", "url": "https://acme.test/mcp"}
                ],
            },
            "tools": None,
        },
    )


def _skill_detail():
    return RegistryRecordDetail(
        record_id="rec-3",
        name="pdf_tables",
        description="original description",
        descriptor_type="AGENT_SKILLS",
        status="APPROVED",
        descriptor_content={
            "skillMd": "---\nname: pdf_tables\n---\nExtract tables.",
            "skillDefinition": {"kind": "extraction"},
        },
    )


def test_description_is_wrapped_in_optional_value():
    service, stub = _service(_detail())
    service.update_record("rec-1", UpdateRecordRequest(description="a better blurb"))
    assert stub.calls[0]["description"] == {"optionalValue": "a better blurb"}


def test_untouched_fields_are_omitted_entirely():
    service, stub = _service(_detail())
    service.update_record("rec-1", UpdateRecordRequest(description="only this"))
    assert "name" not in stub.calls[0]


def test_name_maps_to_display_name_wrapped():
    # req.name is the human-facing display name; the dedup `name` key is
    # immutable once created, so it is never sent here.
    service, stub = _service(_detail())
    service.update_record("rec-1", UpdateRecordRequest(name="agent_two"))
    assert stub.calls[0]["displayName"] == {"optionalValue": "agent_two"}
    assert "name" not in stub.calls[0]


def test_editing_a_deprecated_record_is_refused():
    service, _ = _service(_detail(status="DEPRECATED"))
    with pytest.raises(ValueError, match="DEPRECATED"):
        service.update_record("rec-1", UpdateRecordRequest(description="nope"))


def _sent_descriptor(stub):
    """
    The descriptor content of the edit, with UpdateRegistryRecord's PATCH wrappers
    peeled off.

    Update wraps every independently-unsettable level in its own `optionalValue`
    — the union, each type's primary descriptor key, and every field inside it
    (data/dataSchemaVersion/additionalData), recursing one level further for
    additionalData's own nested fields (MCP's `tools`, AGENT_SKILLS' `skillMd`).
    The shape itself is asserted in test_update_descriptor_shape.py against real
    botocore validation; here it is unwrapped so each test can read the content
    it is actually about.
    """

    def unwrap_fields(fields):
        unwrapped = {}
        for key, wrapper in fields.items():
            value = wrapper["optionalValue"]
            if key == "additionalData":
                unwrapped[key] = {
                    inner_key: unwrap_fields(inner_wrapper["optionalValue"])
                    for inner_key, inner_wrapper in value.items()
                }
            else:
                unwrapped[key] = value
        return unwrapped

    union = stub.calls[0]["descriptors"]["optionalValue"]
    return {
        key: unwrap_fields(wrapper["optionalValue"]) for key, wrapper in union.items()
    }


def test_a2a_edit_preserves_the_runtime_binding():
    # Editing only the description must not drop the runtime ARN that makes the
    # record chattable, and _build_descriptors refuses to build an A2A card
    # without one.
    service, stub = _service(_detail())
    service.update_record("rec-1", UpdateRecordRequest(description="new words"))
    card = json.loads(_sent_descriptor(stub)["a2aAgentCard"]["data"])
    assert card["agentRuntimeArn"] == RUNTIME_ARN
    assert card["url"] == RUNTIME_ARN
    assert card["description"] == "new words"


def test_a2a_edit_keeps_the_descriptor_version():
    # current.version is the record revision, not the agent card's semver.
    service, stub = _service(_detail())
    service.update_record("rec-1", UpdateRecordRequest(description="new words"))
    card = json.loads(_sent_descriptor(stub)["a2aAgentCard"]["data"])
    assert card["version"] == "2.3"


def test_mcp_edit_preserves_the_endpoint():
    # _extract_descriptor_content nests MCP under "server"; merging that shape
    # in flat would bury the remotes and lose the endpoint entirely.
    service, stub = _service(_mcp_detail())
    service.update_record("rec-2", UpdateRecordRequest(description="new words"))
    server = json.loads(_sent_descriptor(stub)["mcpServer"]["data"])
    assert server["remotes"] == [
        {"type": "streamable-http", "url": "https://acme.test/mcp"}
    ]
    assert server["version"] == "1.4.0"
    assert "server" not in server and "tools" not in server


def test_mcp_edit_clips_the_descriptor_description_to_the_schema_limit():
    # The record's own description may run to 4096 characters, but the copy inside
    # the MCP descriptor is validated against server.json, which stops at 100.
    # Sending it through unclipped made every description edit of an MCP record
    # fail with ValidationException — a 502 in the UI — while A2A edits worked.
    service, stub = _service(_mcp_detail())
    long_description = "AgentCore built-in tools: web search, code interpreter, browser. " * 3
    service.update_record("rec-2", UpdateRecordRequest(description=long_description))
    assert stub.calls[0]["description"] == {"optionalValue": long_description}
    server = json.loads(_sent_descriptor(stub)["mcpServer"]["data"])
    assert len(server["description"]) <= 100
    assert server["description"].startswith(long_description[:60])


def test_skill_edit_preserves_the_existing_markdown():
    # Falling through to the generated stub would replace the real SKILL.md.
    service, stub = _service(_skill_detail())
    service.update_record("rec-3", UpdateRecordRequest(description="new words"))
    skills = _sent_descriptor(stub)["agentSkillsDefinition"]
    assert skills["additionalData"]["skillMd"]["data"] == (
        "---\nname: pdf_tables\n---\nExtract tables."
    )
    definition = json.loads(skills["data"])
    assert definition["kind"] == "extraction"
    assert "skillMd" not in definition


def test_renaming_updates_the_descriptor_copy_of_the_name():
    # The descriptor is indexed too, so a stale copy of the name would keep
    # matching the old one.
    service, stub = _service(_detail())
    service.update_record("rec-1", UpdateRecordRequest(name="agent_two"))
    card = json.loads(_sent_descriptor(stub)["a2aAgentCard"]["data"])
    assert card["name"] == "agent_two"


def test_content_override_wins_over_the_existing_payload():
    service, stub = _service(_mcp_detail())
    service.update_record(
        "rec-2",
        UpdateRecordRequest(content={"remotes": [{"type": "sse", "url": "https://b/"}]}),
    )
    server = json.loads(_sent_descriptor(stub)["mcpServer"]["data"])
    assert server["remotes"] == [{"type": "sse", "url": "https://b/"}]
