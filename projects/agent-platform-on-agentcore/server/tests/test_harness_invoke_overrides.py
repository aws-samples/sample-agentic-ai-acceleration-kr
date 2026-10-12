"""
A chat turn can override the harness's model and system prompt for that turn.

InvokeHarness takes `model` and `systemPrompt` per request (measured 2026-09-23
on botocore 1.43.56, the pinned floor: a Haiku harness answered as Sonnet 5 with
a different token count, and a pirate prompt was obeyed). The override is
ephemeral — no new harness version — which is what a per-thread "try this agent
on another model" wants.

The model block is replaced wholesale, so an override that names only the model
would drop the harness's own `maxTokens` and fall back to Bedrock's 4096 default,
the exact failure HARNESS_MAX_TOKENS exists to prevent. The override therefore
carries the cap too.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.harness_client import HarnessClient  # noqa: E402
from models.harness import HARNESS_MAX_TOKENS  # noqa: E402

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
VALUES = {"messages": [{"id": "1", "type": "human", "content": "hello"}]}


class StubAgentCore:
    def __init__(self):
        self.params = None

    def invoke_harness(self, **params):
        self.params = params
        return {"stream": []}


def invoke(config):
    client = HarnessClient(harness_arn=HARNESS_ARN, region_name="us-east-1")
    stub = StubAgentCore()
    client.agentcore_client = stub

    async def drain():
        async for _ in client.execute_stream(
            thread_id="t-1", values=VALUES, config=config, actor_id="u-1"
        ):
            pass

    asyncio.run(drain())
    return stub.params


def test_a_model_override_rides_in_the_request():
    params = invoke({"model_id": "global.anthropic.claude-sonnet-5"})
    assert params["model"]["bedrockModelConfig"]["modelId"] == (
        "global.anthropic.claude-sonnet-5"
    )


def test_the_override_keeps_the_per_call_output_cap():
    """Replacing the model block without a cap reinstates Bedrock's 4096."""
    params = invoke({"model_id": "global.anthropic.claude-sonnet-5"})
    assert params["model"]["bedrockModelConfig"]["maxTokens"] == HARNESS_MAX_TOKENS


def test_no_override_leaves_the_harness_definition_in_charge():
    params = invoke({})
    assert "model" not in params
    assert "systemPrompt" not in params


def test_blank_overrides_are_ignored():
    """A cleared form field arrives as an empty string; that is not an override."""
    params = invoke({"model_id": "", "system_prompt": "   "})
    assert "model" not in params
    assert "systemPrompt" not in params


def test_a_system_prompt_override_is_a_single_text_block():
    params = invoke({"system_prompt": "You are a pirate."})
    assert params["systemPrompt"] == [{"text": "You are a pirate."}]


def test_the_override_survives_botocore_validation():
    import pytest

    boto3 = pytest.importorskip("boto3")
    from botocore.exceptions import ParamValidationError

    client = boto3.client(
        "bedrock-agentcore",
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )
    params = invoke(
        {"model_id": "global.anthropic.claude-sonnet-5", "system_prompt": "hi"}
    )
    op = client.meta.service_model.operation_model("InvokeHarness")
    try:
        client._serializer.serialize_to_request(params, op)
    except ParamValidationError as exc:
        pytest.fail(f"InvokeHarness rejects the params we build: {exc}")
