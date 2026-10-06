"""
UpdateRegistryRecordStatus requires a statusReason.

botocore validates it client-side, so update_status("deprecate") raised
ParamValidationError before any request was sent. Callers that swallow errors
made this invisible: delete_harness logged a warning and reported
`deprecated_records: []` while the record stayed APPROVED — the registry kept
advertising an agent whose harness was gone.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.registry_service import RegistryService  # noqa: E402


class RecordingControl:
    def __init__(self):
        self.status_calls = []

    def update_registry_record_status(self, **params):
        # Mirror botocore's client-side validation of required members.
        for member in ("registryId", "recordId", "status", "statusReason"):
            if not params.get(member):
                raise AssertionError(f"missing required parameter {member}")
        self.status_calls.append(params)
        return {}

    def get_registry_record(self, registryId, recordId):
        return {"recordId": recordId, "name": "r", "status": "DEPRECATED"}


def build():
    service = RegistryService(registry_id="reg-1", region="us-east-1")
    control = RecordingControl()
    service._registry_control = control
    return service, control


@pytest.mark.parametrize("action,status", [
    ("approve", "APPROVED"),
    ("reject", "REJECTED"),
    ("deprecate", "DEPRECATED"),
])
def test_every_status_action_sends_a_reason(action, status):
    service, control = build()

    service.update_status("rec-1", action)

    assert len(control.status_calls) == 1
    call = control.status_calls[0]
    assert call["status"] == status
    assert call["statusReason"].strip()


def test_a_caller_supplied_reason_is_passed_through():
    service, control = build()

    service.update_status("rec-1", "deprecate", reason="Harness was deleted")

    assert control.status_calls[0]["statusReason"] == "Harness was deleted"
