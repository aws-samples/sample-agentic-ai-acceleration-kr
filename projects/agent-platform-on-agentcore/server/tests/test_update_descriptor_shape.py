"""
That an edit's descriptors are shaped the way UpdateRegistryRecord accepts.

Every other test in this suite stubs the boto3 client, which means botocore's
client-side parameter validation never runs — and that is precisely the check that
matters here. UpdateRegistryRecord does not take the same descriptor shape as
CreateRegistryRecord: it wraps every independently-unsettable level in
`optionalValue`, and a create-shaped payload is rejected before it leaves the
process with "Unknown parameter … must be one of: optionalValue".

That gap let AGENT_SKILLS records become uneditable while the suite stayed green.
So these run the real validator over the real parameters, with only the HTTP call
itself intercepted.
"""
import os
import sys

import boto3
import pytest
from botocore.exceptions import ParamValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.registry import (  # noqa: E402
    DESCRIPTOR_A2A,
    DESCRIPTOR_AGENT_SKILLS,
    DESCRIPTOR_CUSTOM,
    DESCRIPTOR_MCP,
    RegistryRecordDetail,
    UpdateRecordRequest,
)
from services.registry_service import RegistryService  # noqa: E402

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/agent-abc"


class ValidatingControl:
    """
    A real boto3 client with only the transport removed.

    Serialisation and parameter validation still run, so an ill-shaped descriptor
    raises exactly as it would against AWS; nothing reaches the network.
    """

    def __init__(self):
        self.client = boto3.client(
            "agent-registry-control",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
            endpoint_url="https://agent-registry-control.us-east-1.api.aws",
        )
        self.calls = []
        # Fail the request after validation and serialisation, before any socket.
        self.client.meta.events.register(
            "before-send.agent-registry-control.UpdateRegistryRecord",
            self._intercept,
        )

    def _intercept(self, request, **_):
        raise _Sent(request)

    def update_registry_record(self, **params):
        self.calls.append(params)
        try:
            self.client.update_registry_record(**params)
        except _Sent:
            # Validation passed; the shape is acceptable to botocore.
            return {}
        raise AssertionError("expected the request to be intercepted before sending")


class _Sent(Exception):
    """Marks a request that survived validation."""


def service_for(detail):
    service = RegistryService(registry_id="reg-1", region="us-east-1")
    control = ValidatingControl()
    service._registry_control = control
    service.get_record = lambda record_id: detail
    return service, control


def a2a_detail():
    return RegistryRecordDetail(
        record_id="rec-1",
        name="agent_one",
        description="original",
        descriptor_type=DESCRIPTOR_A2A,
        status="APPROVED",
        agent_runtime_arn=RUNTIME_ARN,
        descriptor_content={
            "name": "agent_one",
            "version": "2.3",
            "url": RUNTIME_ARN,
            "agentRuntimeArn": RUNTIME_ARN,
        },
    )


def mcp_detail():
    return RegistryRecordDetail(
        record_id="rec-2",
        name="acme/tools",
        description="original",
        descriptor_type=DESCRIPTOR_MCP,
        status="APPROVED",
        descriptor_content={
            "server": {
                "name": "acme/tools",
                "version": "1.4.0",
                "remotes": [{"type": "streamable-http", "url": "https://acme.test/mcp"}],
            },
            "tools": None,
        },
    )


def skill_detail():
    return RegistryRecordDetail(
        record_id="rec-3",
        name="pdf-processing",
        description="original",
        descriptor_type=DESCRIPTOR_AGENT_SKILLS,
        status="APPROVED",
        descriptor_content={
            "skillMd": "---\nname: pdf-processing\ndescription: d\n---\n\nbody",
            "skillDefinition": {
                "_meta": {
                    "com.amazonaws.bap/skillSource": {
                        "type": "s3",
                        "uri": "s3://ap-skills/skills/pdf-processing/",
                    }
                }
            },
        },
    )


def custom_detail():
    return RegistryRecordDetail(
        record_id="rec-4",
        name="lambda-thing",
        description="original",
        descriptor_type=DESCRIPTOR_CUSTOM,
        status="APPROVED",
        descriptor_content={"name": "lambda-thing", "arn": "arn:aws:lambda:::f"},
    )


@pytest.mark.parametrize(
    "detail_factory, record_id",
    [
        (a2a_detail, "rec-1"),
        (mcp_detail, "rec-2"),
        (skill_detail, "rec-3"),
        (custom_detail, "rec-4"),
    ],
    ids=["a2a", "mcp", "agent_skills", "custom"],
)
def test_a_description_edit_passes_botocore_validation(detail_factory, record_id):
    """One case per record type: all four go through the same rewrap."""
    service, _ = service_for(detail_factory())

    service.update_record(record_id, UpdateRecordRequest(description="new words"))


def test_replacing_a_skill_bundle_passes_botocore_validation():
    """The path that was broken: skillMd and skillDefinition wrapped individually."""
    service, _ = service_for(skill_detail())

    service.update_record(
        "rec-3",
        UpdateRecordRequest(
            skill_markdown="---\nname: pdf-processing\ndescription: d2\n---\n\nnew",
            skill_source={
                "type": "s3",
                "uri": "s3://ap-skills/skills/pdf-processing/",
                "files": ["SKILL.md", "references/R.md"],
            },
        ),
    )


def test_the_create_shape_is_what_botocore_rejects():
    """
    Pins the failure this guards against, so the wrapping is demonstrably load
    bearing rather than decoration.
    """
    control = ValidatingControl()

    with pytest.raises(ParamValidationError, match="optionalValue"):
        control.update_registry_record(
            registryId="reg-1",
            recordId="rec-3",
            # CreateRegistryRecord's shape: fields sent bare, with no
            # `optionalValue` anywhere.
            descriptors={
                "agentSkillsDefinition": {
                    "data": "{}",
                    "dataSchemaVersion": "0.1.0",
                    "additionalData": {
                        "skillMd": {"data": "---\nname: a\n---\n"}
                    },
                }
            },
        )
