"""Per-agent application inference profiles (AIP) for cost attribution.

When MODEL_COST_AIP_ENABLED is ON, each harness routes its model calls through
a per-agent AIP ARN so Bedrock model spend is attributable per agent. Any failure
falls back to the base model id — a cost-attribution nicety must never break
harness creation.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import PLATFORM  # noqa: E402
from models.harness import ComposeHarnessRequest  # noqa: E402
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


class StubBedrock:
    """Stubs bedrock client for testing AIP creation."""

    def __init__(self):
        self.create_inference_profile_result = None
        self.create_error = None
        self.last_tags = []

    def create_inference_profile(self, **kwargs):
        self.last_tags = kwargs.get("tags", [])
        if self.create_error:
            raise self.create_error
        if self.create_inference_profile_result:
            return self.create_inference_profile_result
        return {
            "inferenceProfileArn": f"arn:aws:bedrock:us-east-1:1:application-inference-profile/{kwargs.get('inferenceProfileName', 'unknown')}"
        }

    def list_inference_profiles(self, **kwargs):
        return {"inferenceProfileSummaries": []}

    def get_inference_profile(self, **kwargs):
        return {
            "inferenceProfileArn": f"arn:aws:bedrock:us-east-1:1:application-inference-profile/{kwargs.get('inferenceProfileName', 'unknown')}"
        }


def test_aip_arn_used_as_model_id_when_enabled(monkeypatch):
    """When flag is ON, modelId should be the AIP ARN with correct tags."""
    monkeypatch.setattr(
        "services.harness_service.MODEL_COST_AIP_ENABLED", True
    )

    stub_bedrock = StubBedrock()
    stub_bedrock.create_inference_profile_result = {
        "inferenceProfileArn": "arn:aws:bedrock:us-east-1:1:application-inference-profile/writer-haiku"
    }

    service = HarnessService(
        registry=object(),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )
    service._bedrock = stub_bedrock

    mid = service._model_id_for("global.anthropic.claude-haiku-4-5", "writer")

    assert mid == "arn:aws:bedrock:us-east-1:1:application-inference-profile/writer-haiku"
    tags = {t["key"]: t["value"] for t in stub_bedrock.last_tags}
    assert tags["AgentName"] == "writer"
    assert tags["Platform"] == PLATFORM


def test_falls_back_to_base_model_when_disabled(monkeypatch):
    """When flag is OFF, should return base model_id unchanged."""
    monkeypatch.setattr(
        "services.harness_service.MODEL_COST_AIP_ENABLED", False
    )

    service = HarnessService(
        registry=object(),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )

    result = service._model_id_for("m1", "writer")
    assert result == "m1"


def test_falls_back_to_base_model_on_create_error(monkeypatch):
    """When create_inference_profile raises, should fallback to base model_id."""
    monkeypatch.setattr(
        "services.harness_service.MODEL_COST_AIP_ENABLED", True
    )

    stub_bedrock = StubBedrock()
    stub_bedrock.create_error = RuntimeError("nope")

    service = HarnessService(
        registry=object(),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )
    service._bedrock = stub_bedrock

    result = service._model_id_for("m1", "writer")
    assert result == "m1"


def test_reuses_existing_aip_on_conflict(monkeypatch):
    """When profile already exists (ConflictException), look it up and reuse its ARN."""
    monkeypatch.setattr(
        "services.harness_service.MODEL_COST_AIP_ENABLED", True
    )

    stub_bedrock = StubBedrock()
    existing_arn = "arn:aws:bedrock:us-east-1:1:application-inference-profile/bap-writer"

    def create_with_conflict(**kwargs):
        from botocore.exceptions import ClientError
        error = ClientError(
            {"Error": {"Code": "ConflictException", "Message": "Already exists"}},
            "CreateInferenceProfile"
        )
        raise error

    def list_for_reuse(**kwargs):
        return {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileName": f"{PLATFORM}-writer",
                    "inferenceProfileArn": existing_arn,
                }
            ]
        }

    stub_bedrock.create_inference_profile = create_with_conflict
    stub_bedrock.list_inference_profiles = list_for_reuse

    service = HarnessService(
        registry=object(),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )
    service._bedrock = stub_bedrock

    result = service._model_id_for("m1", "writer")
    assert result == existing_arn
