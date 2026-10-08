"""
Tests that deleting a harness stops the registry advertising it.

Without cleanup the registry keeps an APPROVED record whose harness is gone, and
chat bound to that record only fails at invoke time.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.registry import (  # noqa: E402
    DESCRIPTOR_A2A,
    RegistryRecordSummary,
)
from services.harness_service import HarnessService  # noqa: E402
from services.registry_service import RegistryService  # noqa: E402

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
COMPANION_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/h-1-companion"


class FakeControl:
    """Minimal bedrock-agentcore-control stub for the harness lifecycle."""

    def __init__(self, harness_payload=None, get_error=None):
        self._payload = harness_payload
        self._get_error = get_error
        self.deleted = []

    def get_harness(self, harnessId):
        if self._get_error:
            raise self._get_error
        return {"harness": self._payload}

    def delete_harness(self, harnessId):
        self.deleted.append(harnessId)
        return {}


class RecordingRegistry(RegistryService):
    """Real deprecate/index logic over an in-memory record list."""

    def __init__(self, records):
        super().__init__(registry_id="reg-1", region="us-east-1")
        self._records = list(records)
        self.status_updates = []

    def list_records(self, descriptor_type=None, status=None):
        return [
            r
            for r in self._records
            if descriptor_type is None or r.descriptor_type == descriptor_type
        ]

    def update_status(self, record_id, action, reason=None):
        self.status_updates.append((record_id, action))
        for index, record in enumerate(self._records):
            if record.record_id == record_id:
                updated = record.model_copy(update={"status": "DEPRECATED"})
                self._records[index] = updated
                return updated
        raise KeyError(record_id)


def harness_payload():
    return {
        "harnessId": "h-1",
        "arn": HARNESS_ARN,
        "harnessName": "research_agent",
        "status": "READY",
        "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeArn": COMPANION_ARN}},
    }


def record(status="APPROVED", **kwargs):
    return RegistryRecordSummary(
        record_id=kwargs.pop("record_id", "rec-1"),
        name="research_agent",
        descriptor_type=DESCRIPTOR_A2A,
        status=status,
        harness_arn=kwargs.pop("harness_arn", HARNESS_ARN),
        agent_runtime_arn=kwargs.pop("agent_runtime_arn", COMPANION_ARN),
    )


def build(records, control=None):
    registry = RecordingRegistry(records)
    service = HarnessService(
        registry=registry,
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )
    service._control = control or FakeControl(harness_payload())
    return service, registry


def test_delete_deprecates_the_bound_record_once():
    """The record is indexed under both ARNs; it must not be deprecated twice."""
    service, registry = build([record()])
    deprecated = service.delete_harness("h-1")

    assert deprecated == ["rec-1"]
    assert registry.status_updates == [("rec-1", "deprecate")]
    assert service._control.deleted == ["h-1"]


def test_delete_leaves_unrelated_records_alone():
    other = record(
        record_id="rec-other",
        harness_arn=None,
        agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/other",
    )
    service, registry = build([record(), other])
    deprecated = service.delete_harness("h-1")

    assert deprecated == ["rec-1"]
    assert "rec-other" not in [rid for rid, _ in registry.status_updates]


def test_already_deprecated_record_is_not_touched_again():
    service, registry = build([record(status="DEPRECATED")])
    assert service.delete_harness("h-1") == []
    assert registry.status_updates == []


def test_delete_still_succeeds_when_the_pre_delete_lookup_fails():
    """Cleanup is best-effort; losing it must not block the delete itself."""
    control = FakeControl(harness_payload(), get_error=RuntimeError("throttled"))
    service, registry = build([record()], control=control)

    assert service.delete_harness("h-1") == []
    assert control.deleted == ["h-1"]
    assert registry.status_updates == []


def test_delete_still_succeeds_when_deprecation_fails():
    service, registry = build([record()])

    def boom(record_id, action):
        raise RuntimeError("registry unavailable")

    registry.update_status = boom
    assert service.delete_harness("h-1") == []
    assert service._control.deleted == ["h-1"]


def test_deprecate_ignores_empty_and_none_arns():
    registry = RecordingRegistry([record()])
    assert registry.deprecate_records_for_arns(None, None) == []
    assert registry.status_updates == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
