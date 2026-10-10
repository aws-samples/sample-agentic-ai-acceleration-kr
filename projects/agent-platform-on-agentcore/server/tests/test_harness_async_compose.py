"""
Composing a harness must answer before the harness is ready.

The synchronous flow held the HTTP response for the whole CreateHarness →
READY → register-in-registry chain (~2min observed). Next.js's proxy cuts
requests at 30s and answers 500 itself, so the caller saw "Internal Server
Error" while the composition actually succeeded. The route now returns as soon
as CreateHarness is accepted and the registry registration runs in the
background once the harness settles.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.harness import ComposeHarnessRequest  # noqa: E402
import services.harness_service as harness_service_module
from services.harness_service import HarnessService  # noqa: E402

HARNESS_ID = "agent_x-abc123"
HARNESS_ARN = f"arn:aws:bedrock-agentcore:us-east-1:1:harness/{HARNESS_ID}"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/harness_agent_x-r1"


class StubControl:
    """CreateHarness answers CREATING; GetHarness reports the final status."""

    def __init__(self, final_status="READY", failure_reason=None):
        self.final_status = final_status
        self.failure_reason = failure_reason
        self.get_calls = 0

    def create_harness(self, **params):
        return {
            "harness": {
                "harnessId": HARNESS_ID,
                "arn": HARNESS_ARN,
                "harnessName": params["harnessName"],
                "status": "CREATING",
            }
        }

    def get_harness(self, harnessId):
        self.get_calls += 1
        harness = {
            "harnessId": HARNESS_ID,
            "arn": HARNESS_ARN,
            "harnessName": "agent_x",
            "status": self.final_status,
        }
        if self.final_status == "READY":
            harness["environment"] = {
                "agentCoreRuntimeEnvironment": {"agentRuntimeArn": RUNTIME_ARN}
            }
        if self.failure_reason:
            harness["failureReason"] = self.failure_reason
        return {"harness": harness}


class RecordingRegistry:
    """Registry stub that records create_record calls."""

    def __init__(self, existing_arns=()):
        self.existing_arns = set(existing_arns)
        self.created = []

    def create_record(self, req, owner=None):
        self.created.append(req)
        return None

    def get_record(self, record_id):
        raise AssertionError("no registry records should be read in these tests")

    def agent_records_by_arn(self, include_deprecated=False):
        return {arn: object() for arn in self.existing_arns}


def service_with(control, registry=None):
    service = HarnessService(
        registry=registry or RecordingRegistry(),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )
    service._control = control
    return service


def test_compose_returns_before_the_harness_is_ready(monkeypatch):
    """The response must not wait on GetHarness polling or the registry."""
    control = StubControl()
    service = service_with(control)
    # Registration is only spawned when the registry is on; do not let a local
    # .env (or its absence) decide what this test asserts.
    monkeypatch.setattr(harness_service_module, "registry_enabled", lambda: True)
    spawned = []
    monkeypatch.setattr(
        service,
        "_spawn_registration",
        lambda harness_id, req, owner=None: spawned.append(harness_id),
    )

    resp = service.compose_and_register(ComposeHarnessRequest(name="agent_x"))

    assert resp.harness.status == "CREATING"
    assert resp.record is None
    assert control.get_calls == 0
    assert spawned == [HARNESS_ID]


def test_background_registration_waits_for_ready_then_registers():
    registry = RecordingRegistry()
    service = service_with(StubControl(), registry)

    service._register_when_ready(
        HARNESS_ID, ComposeHarnessRequest(name="agent_x", description="desc")
    )

    assert len(registry.created) == 1
    req = registry.created[0]
    assert req.name == "agent_x"
    assert req.description == "desc"
    assert req.harness_arn == HARNESS_ARN
    assert req.agent_runtime_arn == RUNTIME_ARN
    assert req.submit_for_approval is True


def test_background_registration_skips_an_already_registered_harness():
    """A manual sync can win the race; a second record must not be created."""
    registry = RecordingRegistry(existing_arns=[HARNESS_ARN])
    service = service_with(StubControl(), registry)

    service._register_when_ready(HARNESS_ID, ComposeHarnessRequest(name="agent_x"))

    assert registry.created == []


def test_background_registration_skips_a_failed_harness():
    registry = RecordingRegistry()
    service = service_with(
        StubControl(final_status="CREATE_FAILED", failure_reason="boom"), registry
    )

    service._register_when_ready(HARNESS_ID, ComposeHarnessRequest(name="agent_x"))

    assert registry.created == []


def test_background_registration_survives_registry_errors():
    """The harness exists either way; a registry failure must only be logged."""

    class ExplodingRegistry(RecordingRegistry):
        def create_record(self, req, owner=None):
            raise RuntimeError("registry down")

    service = service_with(StubControl(), ExplodingRegistry())

    # Must not raise — this runs on a daemon thread with no one to catch it.
    service._register_when_ready(HARNESS_ID, ComposeHarnessRequest(name="agent_x"))
