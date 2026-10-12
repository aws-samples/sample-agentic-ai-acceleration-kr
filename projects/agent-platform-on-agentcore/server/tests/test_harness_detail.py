"""
GetHarness has to come back with enough to refill the compose form.

Editing a harness starts from what it is now. The summary used to carry only the
model id and the raw `tools`/`skills` lists; the edit form needs the system
prompt, the loop limits, truncation, memory and — for reverse-mapping onto the
catalogue — each tool's identifying source (gateway ARN, MCP URL, skill URI).
Shapes below are what GetHarness actually returned for `web_harness` on
2026-09-23.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.harness_service import _to_harness_summary  # noqa: E402

GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/bap-gateway-gwexample01"

GET_HARNESS = {
    "harnessId": "web_harness-UvWxY11223",
    "harnessName": "web_harness",
    "arn": "arn:aws:bedrock-agentcore:us-east-1:1:harness/web_harness-UvWxY11223",
    "status": "READY",
    "harnessVersion": "2",
    "model": {
        "bedrockModelConfig": {
            "modelId": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "maxTokens": 64000,
            "apiFormat": "converse_stream",
        }
    },
    "systemPrompt": [{"text": "You are a helpful assistant."}],
    "tools": [
        {
            "type": "agentcore_gateway",
            "name": "bap-gateway-gwexample01",
            "config": {
                "agentCoreGateway": {
                    "gatewayArn": GATEWAY_ARN,
                    "outboundAuth": {"awsIam": {}},
                }
            },
        },
        {
            "type": "remote_mcp",
            "name": "docs",
            "config": {"remoteMcp": {"url": "https://docs.example/mcp"}},
        },
        {"type": "agentcore_browser", "name": "agentcore_browser"},
    ],
    "skills": [
        {"s3": {"uri": "s3://bap-skills/skills/writer/"}},
        {"awsSkills": {"paths": ["core-skills/*"]}},
    ],
    "allowedTools": ["*"],
    "truncation": {
        "strategy": "sliding_window",
        "config": {"slidingWindow": {"messagesCount": 150}},
    },
    "memory": {
        "managedMemoryConfiguration": {
            "arn": "arn:aws:bedrock-agentcore:us-east-1:1:memory/web_harness-YzAbC38475",
            "strategies": ["SUMMARIZATION"],
            "eventExpiryDuration": 365,
        }
    },
    "maxIterations": 75,
    "timeoutSeconds": 3600,
}


def test_the_prompt_and_loop_limits_are_exposed():
    s = _to_harness_summary(GET_HARNESS)
    assert s.version == "2"
    assert s.system_prompt == "You are a helpful assistant."
    assert s.max_tokens == 64000
    assert s.max_iterations == 75
    assert s.timeout_seconds == 3600
    assert s.allowed_tools == ["*"]


def test_truncation_and_memory_are_flattened_for_the_form():
    s = _to_harness_summary(GET_HARNESS)
    assert s.truncation.strategy == "sliding_window"
    assert s.truncation.messages_count == 150
    assert s.memory.strategies == ["SUMMARIZATION"]
    assert s.memory.event_expiry_days == 365


def test_each_tool_kind_is_reported_by_its_identifying_source():
    """The form re-selects catalogue entries by these, not by tool name."""
    s = _to_harness_summary(GET_HARNESS)
    assert s.gateway_arns == [GATEWAY_ARN]
    assert s.mcp_urls == ["https://docs.example/mcp"]
    assert s.builtin_tools == ["agentcore_browser"]
    assert s.skill_uris == ["s3://bap-skills/skills/writer/"]
    assert s.aws_skill_paths == ["core-skills/*"]


def test_a_listing_row_leaves_the_detail_fields_unset():
    """ListHarnesses omits all of this; None (not []) keeps `needs_hydration`
    able to tell "not fetched" from "fetched, empty"."""
    s = _to_harness_summary(
        {"harnessId": "x-1", "arn": "arn:x", "harnessName": "x", "status": "READY"}
    )
    assert s.tools is None and s.skills is None
    assert s.system_prompt is None
    assert s.truncation is None and s.memory is None
    assert s.gateway_arns is None and s.mcp_urls is None
    assert s.builtin_tools is None and s.skill_uris is None


def test_a_fetched_harness_with_no_tools_reports_empty_lists():
    s = _to_harness_summary({**GET_HARNESS, "tools": [], "skills": []})
    assert s.gateway_arns == [] and s.mcp_urls == [] and s.builtin_tools == []
    assert s.skill_uris == [] and s.aws_skill_paths == []
