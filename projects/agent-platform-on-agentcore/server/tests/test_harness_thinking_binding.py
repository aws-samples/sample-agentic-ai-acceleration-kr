"""
A harness on a preserved-thinking model must not die on a thinking signature.

Claude Sonnet 5.5 / Opus 5.5 / Haiku 5.5 / Fable 5.1+ bind each `thinking`
block's signature to the conversation that produced it. The harness loop runs
on AWS's side and rebuilds every assistant message from the stream before it
sends the next model call. When one response carries two thinking blocks, that
rebuild can hand the second block back with a signature the API no longer
accepts, and the call fails with the 400 seen in chat:

    messages.1.content.6: Invalid `signature` in `thinking` block. The block is
    bound to a different conversation. ... set
    `thinking.block_binding.prefix_mismatch_behavior` to "drop_block".

The rebuild is not ours to fix, so the harness asks the model to drop such a
block and keep going instead of failing the turn (measured 2026-11-12 in
ap-northeast-1: the same corrupted history is a 400 with
`prefix_mismatch_behavior: "error"` and a 200 with `"drop_block"`).

`additionalParams` is spread into the ConverseStream call as top-level keyword
arguments — `{"thinking": ...}` there is rejected as an unknown ConverseStream
parameter — so the fields ride inside `additionalModelRequestFields`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.harness import ComposeHarnessRequest, UpdateHarnessRequest  # noqa: E402
from services import harness_service as harness_service_module  # noqa: E402
from test_harness_invoke_overrides import invoke  # noqa: E402
from test_harness_max_tokens import RecordingControl, service_with  # noqa: E402

SONNET_55 = "global.anthropic.claude-sonnet-5-5"
SONNET_5 = "global.anthropic.claude-sonnet-5"

DROP_BLOCK = {
    "additionalModelRequestFields": {
        "thinking": {
            "type": "adaptive",
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        },
        "anthropic_beta": ["thinking-binding-controls-2026-08-01"],
    }
}


def created(model_id: str) -> dict:
    control = RecordingControl()
    service_with(control).create_harness(
        ComposeHarnessRequest(name="academic_writer", model_id=model_id)
    )
    return control.params["model"]["bedrockModelConfig"]


class UpdatingControl:
    def __init__(self):
        self.params = None

    def update_harness(self, **params):
        self.params = params
        return {"harness": {"harnessId": params["harnessId"], "status": "UPDATING"}}


def updated(model_id: str) -> dict:
    control = UpdatingControl()
    service_with(control).update_harness(
        "writer-abc123", UpdateHarnessRequest(model_id=model_id)
    )
    return control.params["model"]["bedrockModelConfig"]


def test_a_preserved_thinking_harness_drops_a_broken_block_instead_of_failing():
    assert created(SONNET_55)["additionalParams"] == DROP_BLOCK


def test_the_fields_ride_inside_additional_model_request_fields():
    """At the top level, ConverseStream rejects `thinking` as an unknown parameter."""
    params = created(SONNET_55)["additionalParams"]
    assert set(params) == {"additionalModelRequestFields"}


def test_every_preserved_thinking_model_gets_it():
    for model_id in (
        "global.anthropic.claude-opus-5-5",
        "global.anthropic.claude-haiku-5-5",
        "us.anthropic.claude-sonnet-5-5",
        "anthropic.claude-fable-5-2",
        "global.anthropic.claude-fable-5-1",
    ):
        assert created(model_id).get("additionalParams") == DROP_BLOCK, model_id


def test_a_model_without_preserved_thinking_is_left_alone():
    """block_binding is beta; an older model has no use for it."""
    for model_id in (
        SONNET_5,
        "global.anthropic.claude-opus-5",
        "global.anthropic.claude-haiku-4-5",
        "amazon.nova-pro-v1:0",
    ):
        assert "additionalParams" not in created(model_id), model_id


def test_an_inference_profile_arn_is_judged_by_the_model_it_copies(monkeypatch):
    """With AIP on, modelId is a profile ARN that no longer names the model."""
    monkeypatch.setattr(harness_service_module, "MODEL_COST_AIP_ENABLED", True)
    monkeypatch.setattr(
        harness_service_module.HarnessService,
        "_model_id_for",
        lambda self, model_id, agent_name: "arn:aws:bedrock:us-east-1:1:application-inference-profile/p1",
    )
    config = created(SONNET_55)
    assert config["modelId"].startswith("arn:aws:bedrock:")
    assert config["additionalParams"] == DROP_BLOCK


def test_an_update_keeps_it():
    """UpdateHarness replaces the model block; omitting it would remove it."""
    assert updated(SONNET_55)["additionalParams"] == DROP_BLOCK


def test_a_per_turn_model_override_keeps_it():
    """InvokeHarness also swaps the model block wholesale."""
    config = invoke({"model_id": SONNET_55})["model"]["bedrockModelConfig"]
    assert config["additionalParams"] == DROP_BLOCK


def test_a_per_turn_override_to_an_older_model_sends_none():
    config = invoke({"model_id": SONNET_5})["model"]["bedrockModelConfig"]
    assert "additionalParams" not in config


def test_create_and_update_requests_still_pass_botocore_validation():
    import boto3
    from botocore.exceptions import ParamValidationError

    client = boto3.client(
        "bedrock-agentcore-control",
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )
    control = RecordingControl()
    service_with(control).create_harness(
        ComposeHarnessRequest(name="academic_writer", model_id=SONNET_55)
    )
    update = UpdatingControl()
    service_with(update).update_harness("writer-abc123", UpdateHarnessRequest(model_id=SONNET_55))
    for op_name, params in (("CreateHarness", control.params), ("UpdateHarness", update.params)):
        op = client.meta.service_model.operation_model(op_name)
        try:
            client._serializer.serialize_to_request(params, op)
        except ParamValidationError as exc:
            raise AssertionError(f"{op_name} rejects the params we build: {exc}")



# --- harnesses composed before this setting existed ---------------------------
#
# A deployment that upgrades this server still has harnesses whose stored model
# block lacks the setting. Nobody should have to run a migration for them: the
# chat path reads the harness's stored block and, when the setting is missing,
# sends the block back with it for that request.

import asyncio  # noqa: E402

from agents.harness_client import HarnessClient  # noqa: E402
from models.harness import with_preserved_thinking_params  # noqa: E402

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
PROFILE_ARN = "arn:aws:bedrock:us-east-1:1:application-inference-profile/p1"


def test_a_stored_block_without_it_gets_it_and_keeps_everything_else():
    stored = {"modelId": SONNET_55, "maxTokens": 16000, "apiFormat": "converse_stream"}
    assert with_preserved_thinking_params(stored) == {**stored, "additionalParams": DROP_BLOCK}


def test_a_stored_block_that_has_it_or_does_not_need_it_is_left_alone():
    done = {"modelId": SONNET_55, "maxTokens": 64000, "additionalParams": DROP_BLOCK}
    assert with_preserved_thinking_params(done) is None
    assert with_preserved_thinking_params({"modelId": SONNET_5, "maxTokens": 64000}) is None


def test_other_additional_params_survive_the_merge():
    stored = {
        "modelId": SONNET_55,
        "additionalParams": {
            "serviceTier": {"type": "priority"},
            "additionalModelRequestFields": {
                "thinking": {"type": "adaptive", "display": "summarized"},
                "anthropic_beta": ["some-other-beta"],
            },
        },
    }
    params = with_preserved_thinking_params(stored)["additionalParams"]
    assert params["serviceTier"] == {"type": "priority"}
    fields = params["additionalModelRequestFields"]
    assert fields["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert fields["anthropic_beta"] == [
        "some-other-beta",
        "thinking-binding-controls-2026-08-01",
    ]


def test_a_thinking_mode_that_rejects_block_binding_is_left_alone():
    """`between_tools` with `block_binding` is a 400."""
    stored = {
        "modelId": SONNET_55,
        "additionalParams": {
            "additionalModelRequestFields": {"thinking": {"type": "between_tools"}}
        },
    }
    assert with_preserved_thinking_params(stored) is None


def harness_reply(model_config, status="READY"):
    return {"harness": {"status": status, "model": {"bedrockModelConfig": model_config}}}


class StubControl:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, 0

    def get_harness(self, harnessId):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


class StubBedrock:
    def get_inference_profile(self, inferenceProfileIdentifier):
        return {
            "models": [
                {"modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{SONNET_55}"}
            ]
        }


def chat(control, config=None, qualifier=None):
    """One chat turn against a harness whose stored definition `control` serves."""
    client = HarnessClient(harness_arn=HARNESS_ARN, qualifier=qualifier, region_name="us-east-1")

    class StubAgentCore:
        params = None

        def invoke_harness(self, **params):
            StubAgentCore.params = params
            return {"stream": []}

    client.agentcore_client = StubAgentCore()
    client.control_client = control
    client.bedrock_client = StubBedrock()

    async def drain():
        async for _ in client.execute_stream(
            thread_id="t-1",
            values={"messages": [{"id": "1", "type": "human", "content": "hello"}]},
            config=config or {},
            actor_id="u-1",
        ):
            pass

    asyncio.run(drain())
    return StubAgentCore.params


def test_a_harness_composed_before_the_setting_is_healed_on_each_turn():
    stored = {"modelId": SONNET_55, "maxTokens": 16000}
    params = chat(StubControl(harness_reply(stored)))
    assert params["model"]["bedrockModelConfig"] == {**stored, "additionalParams": DROP_BLOCK}


def test_a_harness_that_already_has_it_is_left_in_charge():
    stored = {"modelId": SONNET_55, "maxTokens": 64000, "additionalParams": DROP_BLOCK}
    assert "model" not in chat(StubControl(harness_reply(stored)))


def test_a_legacy_harness_on_an_inference_profile_is_healed_too():
    params = chat(StubControl(harness_reply({"modelId": PROFILE_ARN, "maxTokens": 64000})))
    config = params["model"]["bedrockModelConfig"]
    assert config["modelId"] == PROFILE_ARN
    assert config["additionalParams"] == DROP_BLOCK


def test_a_failed_read_never_fails_the_turn():
    params = chat(StubControl(error=RuntimeError("throttled")))
    assert "model" not in params
    assert params["harnessArn"] == HARNESS_ARN


def test_a_harness_mid_update_is_not_overridden():
    """DEFAULT still serves the old version; the new block is not live yet."""
    stored = {"modelId": SONNET_55, "maxTokens": 16000}
    assert "model" not in chat(StubControl(harness_reply(stored, status="UPDATING")))


def test_a_pinned_endpoint_is_not_overridden():
    """A named endpoint may serve an older version with another model."""
    stored = {"modelId": SONNET_55, "maxTokens": 16000}
    assert "model" not in chat(StubControl(harness_reply(stored)), qualifier="canary")


def test_the_stored_block_is_read_once_per_harness_not_once_per_turn():
    control = StubControl(harness_reply({"modelId": SONNET_55, "maxTokens": 16000}))
    chat(control)
    chat(control)
    assert control.calls == 1


def test_a_per_turn_model_override_wins_and_needs_no_read():
    control = StubControl(harness_reply({"modelId": SONNET_55, "maxTokens": 16000}))
    params = chat(control, config={"model_id": SONNET_5})
    assert params["model"]["bedrockModelConfig"]["modelId"] == SONNET_5
    assert control.calls == 0
