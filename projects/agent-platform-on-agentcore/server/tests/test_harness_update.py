"""
A composed harness can be changed in place instead of deleted and recreated.

UpdateHarness is a partial update (measured 2026-09-23: sending only
`systemPrompt` kept the tools, model and memory and produced version 2, which the
DEFAULT endpoint followed on its own). So the service sends exactly what the edit
form owns and nothing it does not — in particular never `memory`, whose
re-submission could recreate the Memory behind a live conversation, and never
`harnessName`/`tags`, which the API has no update for.

The lists it does own (`tools`, `skills`) are replacements, so an empty selection
must be sent as `[]`, not omitted — omitting would silently keep the old tools.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.harness import (  # noqa: E402
    HARNESS_MAX_TOKENS,
    ComposeHarnessRequest,
    TruncationSettings,
    UpdateHarnessRequest,
)
from services.harness_service import HarnessService  # noqa: E402

HARNESS_ID = "writer-abc123"
HARNESS_ARN = f"arn:aws:bedrock-agentcore:us-east-1:1:harness/{HARNESS_ID}"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/gw-1"


class RecordingControl:
    def __init__(self):
        self.update_params = None
        self.create_params = None

    def update_harness(self, **params):
        self.update_params = params
        return {
            "harness": {
                "harnessId": params["harnessId"],
                "arn": HARNESS_ARN,
                "harnessName": "academic_writer",
                "status": "UPDATING",
                "harnessVersion": "2",
            }
        }

    def get_harness(self, harnessId):
        return {
            "harness": {
                "harnessId": harnessId,
                "arn": HARNESS_ARN,
                "harnessName": "academic_writer",
                "status": "READY",
            }
        }

    def create_harness(self, **params):
        self.create_params = params
        return {
            "harness": {
                "harnessId": HARNESS_ID,
                "arn": HARNESS_ARN,
                "harnessName": params["harnessName"],
                "status": "CREATING",
            }
        }


def service_with(control):
    service = HarnessService(
        registry=object(),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
        skills_bucket="bap-skills",
    )
    service._control = control
    service._resolve_mcp_tools = lambda ids: []
    service._resolve_skills = lambda record_ids, paths: (
        [{"awsSkills": {"paths": paths}}] if paths else []
    )
    return service


def updated_with(**kwargs) -> dict:
    control = RecordingControl()
    summary = service_with(control).update_harness(
        HARNESS_ID, UpdateHarnessRequest(**kwargs)
    )
    assert summary.status == "UPDATING"
    return control.update_params


def test_the_update_targets_the_harness_and_never_renames_or_retags_it():
    params = updated_with(system_prompt="Be terse.")
    assert params["harnessId"] == HARNESS_ID
    assert "harnessName" not in params
    assert "tags" not in params


def test_memory_is_never_resubmitted():
    """Re-sending managedMemoryConfiguration risks recreating the Memory."""
    assert "memory" not in updated_with(system_prompt="x")


def test_the_prompt_and_model_are_sent_when_given():
    params = updated_with(
        system_prompt="Be terse.", model_id="global.anthropic.claude-sonnet-5"
    )
    assert params["systemPrompt"] == [{"text": "Be terse."}]
    assert params["model"]["bedrockModelConfig"] == {
        "modelId": "global.anthropic.claude-sonnet-5",
        "maxTokens": HARNESS_MAX_TOKENS,
    }


def test_a_model_change_carries_the_requested_cap():
    params = updated_with(model_id="m", max_tokens=16000)
    assert params["model"]["bedrockModelConfig"]["maxTokens"] == 16000


def test_a_cap_change_alone_still_needs_the_model_id():
    """bedrockModelConfig.modelId is required, so a cap-only edit resends it."""
    params = updated_with(model_id="m", max_tokens=8000)
    assert params["model"]["bedrockModelConfig"]["modelId"] == "m"


def test_tool_and_skill_lists_are_replacements_so_empty_means_empty():
    params = updated_with(builtin_tools=[], gateway_arns=[], skill_bucket_uris=[])
    assert params["tools"] == []
    assert params["skills"] == []


def test_an_edit_that_names_no_selection_leaves_tools_and_skills_alone():
    """A prompt-only edit must not wipe the harness's tools."""
    params = updated_with(system_prompt="x")
    assert "tools" not in params and "skills" not in params


def test_selected_tools_and_skills_are_resolved_like_a_create():
    params = updated_with(
        builtin_tools=["agentcore_browser"],
        gateway_arns=[GATEWAY_ARN],
        aws_skill_paths=["core-skills/*"],
    )
    types = [t["type"] for t in params["tools"]]
    assert "agentcore_browser" in types and "agentcore_gateway" in types
    assert params["skills"] == [{"awsSkills": {"paths": ["core-skills/*"]}}]


def test_a_skill_reached_two_ways_is_attached_once():
    """Registry record and bucket prefix of the same upload resolve to one S3
    source; the harness must not list it twice."""
    control = RecordingControl()
    service = service_with(control)
    service._resolve_skills = lambda record_ids, paths: [
        {"s3": {"uri": "s3://bap-skills/skills/writer/"}}
    ]
    service.update_harness(
        HARNESS_ID,
        UpdateHarnessRequest(
            skill_record_ids=["rec-writer"],
            skill_bucket_uris=["s3://bap-skills/skills/writer"],
        ),
    )
    assert control.update_params["skills"] == [
        {"s3": {"uri": "s3://bap-skills/skills/writer/"}}
    ]


def test_loop_limits_and_truncation_are_sent():
    params = updated_with(
        allowed_tools=["*"],
        max_iterations=40,
        timeout_seconds=900,
        truncation=TruncationSettings(strategy="sliding_window", messages_count=80),
    )
    assert params["allowedTools"] == ["*"]
    assert params["maxIterations"] == 40
    assert params["timeoutSeconds"] == 900
    assert params["truncation"] == {
        "strategy": "sliding_window",
        "config": {"slidingWindow": {"messagesCount": 80}},
    }


def test_a_strategy_without_a_count_sends_no_config():
    params = updated_with(truncation=TruncationSettings(strategy="none"))
    assert params["truncation"] == {"strategy": "none"}


def test_the_update_survives_botocore_validation():
    boto3 = pytest.importorskip("boto3")
    from botocore.exceptions import ParamValidationError

    client = boto3.client(
        "bedrock-agentcore-control",
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )
    params = updated_with(
        system_prompt="x",
        model_id="m",
        builtin_tools=["agentcore_code_interpreter"],
        gateway_arns=[GATEWAY_ARN],
        aws_skill_paths=["core-skills/*"],
        allowed_tools=["*"],
        max_iterations=10,
        timeout_seconds=60,
        truncation=TruncationSettings(strategy="summarization"),
    )
    op = client.meta.service_model.operation_model("UpdateHarness")
    try:
        client._serializer.serialize_to_request(params, op)
    except ParamValidationError as exc:
        pytest.fail(f"UpdateHarness rejects the params we build: {exc}")


# --- the create path gains the same options ---------------------------------


def created_with(**kwargs) -> dict:
    control = RecordingControl()
    service_with(control).create_harness(
        ComposeHarnessRequest(name="academic_writer", **kwargs)
    )
    return control.create_params


def test_create_defaults_are_unchanged():
    params = created_with()
    assert params["truncation"] == {"strategy": "sliding_window"}
    assert params["memory"]["managedMemoryConfiguration"]["eventExpiryDuration"] == 365


def test_create_honours_truncation_and_memory_expiry():
    params = created_with(
        truncation=TruncationSettings(strategy="summarization"),
        memory_event_expiry_days=30,
    )
    assert params["truncation"] == {"strategy": "summarization"}
    assert params["memory"]["managedMemoryConfiguration"]["eventExpiryDuration"] == 30
