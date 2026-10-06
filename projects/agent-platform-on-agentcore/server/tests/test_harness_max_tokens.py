"""
A composed harness must name its per-call output cap.

`bedrockModelConfig.maxTokens` is the ceiling on a *single* model call's output.
Left unset, Bedrock Converse applies its own default — measured at **4096** output
tokens for `global.anthropic.claude-sonnet-5`, against a model that supports
128K. A harness whose turn writes anything substantial (a Node script that builds
a .docx, a long analysis) runs into that ceiling, the call comes back
`stopReason=max_tokens`, and the harness surfaces it as a fatal
`runtimeClientError: Model stopped generating due to maximum token limit` —
the whole turn dies rather than the answer being merely short.

Not to be confused with the request's **top-level** `maxTokens`, which AWS
documents as the total across *all* model calls in one invocation. That is a
loop-wide budget; this is the per-call response limit, and only the latter
governs whether one reply can finish.

Charging is on tokens produced, not on the ceiling, so a generous cap costs
nothing until it is used.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.harness import ComposeHarnessRequest  # noqa: E402
from services import harness_service as harness_service_module  # noqa: E402
from services.harness_service import HarnessService  # noqa: E402

HARNESS_ID = "writer-abc123"
HARNESS_ARN = f"arn:aws:bedrock-agentcore:us-east-1:1:harness/{HARNESS_ID}"


class RecordingControl:
    """Captures the CreateHarness params without calling AWS."""

    def __init__(self):
        self.params = None

    def create_harness(self, **params):
        self.params = params
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
    )
    service._control = control
    # Compose resolves records through the registry; none are requested here.
    service._resolve_mcp_tools = lambda ids: []
    service._resolve_skills = lambda record_ids, paths: []
    return service


def created_with(**kwargs) -> dict:
    control = RecordingControl()
    service_with(control).create_harness(
        ComposeHarnessRequest(name="academic_writer", **kwargs)
    )
    return control.params


def model_config(params: dict) -> dict:
    return params["model"]["bedrockModelConfig"]


def test_a_composed_harness_sets_a_per_call_output_cap():
    """Without it the model call dies at Bedrock's 4096-token default."""
    assert "maxTokens" in model_config(created_with()), (
        "no maxTokens on bedrockModelConfig — the harness inherits Bedrock's "
        "default and a long reply ends as runtimeClientError"
    )


def test_the_default_cap_leaves_room_for_a_long_reply():
    """4096 is the failure being fixed; the model's own ceiling is 128K."""
    cap = model_config(created_with())["maxTokens"]
    assert 4096 < cap <= 128000


def test_a_requested_cap_wins():
    assert model_config(created_with(max_tokens=16000))["maxTokens"] == 16000


def test_the_cap_is_not_written_as_the_invocation_budget():
    """AWS's top-level `maxTokens` is the total across every call in a turn.

    Setting that instead would cap the whole loop rather than one reply, and
    would leave the per-call default — the actual cause — in place.
    """
    params = created_with()
    assert "maxTokens" not in params


def test_the_cap_survives_botocore_validation():
    """A stub cannot catch a shape AWS rejects.

    Serialising through the real service model runs botocore's own parameter
    validation, which is what caught the Update-vs-Create descriptor mismatch
    elsewhere in this codebase.
    """
    boto3 = pytest.importorskip("boto3")
    from botocore.exceptions import ParamValidationError

    client = boto3.client(
        "bedrock-agentcore-control",
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )
    op = client.meta.service_model.operation_model("CreateHarness")
    try:
        client._serializer.serialize_to_request(created_with(), op)
    except ParamValidationError as exc:
        pytest.fail(f"CreateHarness rejects the params we build: {exc}")


def test_the_module_default_is_overridable_without_editing_callers(monkeypatch):
    """The cap is a named constant so a deployment can retune it in one place."""
    monkeypatch.setattr(harness_service_module, "HARNESS_MAX_TOKENS", 32000)
    assert model_config(created_with())["maxTokens"] == 32000
