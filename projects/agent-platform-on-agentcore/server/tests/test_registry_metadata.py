"""
Record fields the GA registry added and this platform now reads or writes:
custom metadata (typed, schema-validated, searchable), provenance (auto-detected
records), `http`/`agui` descriptors (what auto-detection writes for a runtime),
synchronisation sources, and record tags.
"""
import json
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.registry import (  # noqa: E402
    CreateRecordRequest,
    DESCRIPTOR_A2A,
    DESCRIPTOR_MCP,
    RegistryRecordDetail,
    UpdateRecordRequest,
)
from services.registry_service import (  # noqa: E402
    RegistryService,
    _build_descriptors,
    _to_detail,
    _to_summary,
    mcp_endpoint_of,
)

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/bap_default-AbC123"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/bap-gateway-zujfqvy9zs"
INVOCATION_URL = (
    "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/"
    f"{quote(RUNTIME_ARN, safe='')}/invocations?qualifier=DEFAULT"
)


def test_summary_reads_custom_metadata_and_compliance():
    summary = _to_summary(
        {
            "recordId": "r",
            "name": "r",
            "recordType": "MCP",
            "status": "APPROVED",
            "customMetadata": {"team": "search", "tier": "internal"},
            "customMetadataSchemaComplianceStatus": "NON_COMPLIANT",
        }
    )
    assert summary.custom_metadata == {"team": "search", "tier": "internal"}
    assert summary.compliance_status == "NON_COMPLIANT"
    assert summary.auto_detected is False


def test_summary_marks_auto_detected_records_from_provenance():
    summary = _to_summary(
        {
            "recordId": "r",
            "name": "aws-autodetected-123456789012-us-east-1-bap-gateway-zujfqvy9zs",
            "recordType": "GATEWAY",
            "status": "DRAFT",
            "createdByAutoDetection": True,
            "provenance": [
                {
                    "relation": "DETECTED_FROM",
                    "sourceId": GATEWAY_ARN,
                    "sourceType": "AWS::BedrockAgentCore::Gateway",
                }
            ],
        }
    )
    assert summary.auto_detected is True
    assert summary.source_arn == GATEWAY_ARN
    assert summary.source_type == "AWS::BedrockAgentCore::Gateway"
    # A GATEWAY record is a tool surface, like our own MCP records.
    assert summary.descriptor_type == DESCRIPTOR_MCP


def test_http_descriptor_yields_the_runtime_arn_and_qualifier():
    # What auto-detection writes for an HTTP-protocol runtime: no agent card,
    # just the invocation endpoint. The ARN is URL-encoded inside the path.
    detail = _to_detail(
        {
            "recordId": "r",
            "name": "r",
            "recordType": "AGENT",
            "status": "APPROVED",
            "descriptors": {"http": {"source": {"fromUrl": {"url": INVOCATION_URL}}}},
        }
    )
    assert detail.descriptor_type == DESCRIPTOR_A2A
    assert detail.agent_runtime_arn == RUNTIME_ARN
    assert detail.harness_arn is None
    assert detail.qualifier == "DEFAULT"
    assert detail.sync_source == {"url": INVOCATION_URL, "credential": "none"}


def test_sync_source_summarises_an_iam_credential_provider():
    detail = _to_detail(
        {
            "recordId": "r",
            "name": "r",
            "recordType": "MCP",
            "status": "DRAFT",
            "descriptors": {
                "mcpServer": {
                    "data": json.dumps({"name": "bap/x", "description": "x", "version": "1.0.0"}),
                    "dataSchemaVersion": "2025-12-11",
                    "source": {
                        "fromUrl": {
                            "url": "https://g.example/mcp",
                            "credentialProviderConfigurations": [
                                {
                                    "credentialProviderType": "IAM",
                                    "credentialProvider": {
                                        "iamCredentialProvider": {
                                            "roleArn": "arn:aws:iam::1:role/sync",
                                            "service": "agent-registry",
                                        }
                                    },
                                }
                            ],
                        }
                    },
                }
            },
        }
    )
    assert detail.sync_source == {
        "url": "https://g.example/mcp",
        "credential": "iam",
        "role_arn": "arn:aws:iam::1:role/sync",
    }


def test_build_mcp_with_sync_url_adds_source_and_keeps_data():
    # Verified live 2026-10-10: data and source coexist; sync fills tools and
    # preserves unknown keys such as gatewayArn in data.
    req = CreateRecordRequest(
        name="tools",
        descriptor_type=DESCRIPTOR_MCP,
        gateway_arn=GATEWAY_ARN,
        sync_url="https://g.example/mcp",
    )
    d = _build_descriptors(req)
    assert d["mcpServer"]["source"] == {"fromUrl": {"url": "https://g.example/mcp"}}
    assert json.loads(d["mcpServer"]["data"])["gatewayArn"] == GATEWAY_ARN


def test_build_with_sync_role_adds_an_iam_credential_provider_for_agent_registry():
    req = CreateRecordRequest(
        name="tools",
        descriptor_type=DESCRIPTOR_MCP,
        sync_url="https://g.example/mcp",
        sync_role_arn="arn:aws:iam::1:role/sync",
    )
    from_url = _build_descriptors(req)["mcpServer"]["source"]["fromUrl"]
    assert from_url["credentialProviderConfigurations"] == [
        {
            "credentialProviderType": "IAM",
            "credentialProvider": {
                "iamCredentialProvider": {
                    "roleArn": "arn:aws:iam::1:role/sync",
                    # The SigV4 service name AWS documents for servers hosted on
                    # AgentCore Runtime or Gateway.
                    "service": "agent-registry",
                }
            },
        }
    ]


def test_build_a2a_with_sync_url_points_the_card_source_at_the_url():
    req = CreateRecordRequest(
        name="agent",
        descriptor_type=DESCRIPTOR_A2A,
        agent_runtime_arn=RUNTIME_ARN,
        sync_url="https://a.example/.well-known/agent-card.json",
    )
    d = _build_descriptors(req)
    assert d["a2aAgentCard"]["source"]["fromUrl"]["url"].endswith("agent-card.json")


class StubControl:
    def __init__(self, record=None, registry=None):
        self.record = record or {}
        self.registry = registry or {}
        self.created = []
        self.updated = []

    def create_registry_record(self, **params):
        self.created.append(params)
        return {"recordArn": "arn:aws:agent-registry:us-east-1:1:registry/r/record/new1", "status": "CREATING"}

    def get_registry_record(self, **params):
        return {"recordId": params["recordId"], "name": "n", "recordType": "MCP", "status": "DRAFT", **self.record}

    def update_registry_record(self, **params):
        self.updated.append(params)
        return {}

    def get_registry(self, **params):
        return self.registry

    def submit_registry_record_for_approval(self, **params):
        return {}


def _service(record=None, registry=None):
    service = RegistryService(registry_id="reg-1", region="us-east-1")
    stub = StubControl(record, registry)
    service._registry_control = stub
    return service, stub


def test_create_omits_empty_custom_metadata_and_tags():
    # A registry without a schema rejects any customMetadata, even {}.
    service, stub = _service()
    service.create_record(
        CreateRecordRequest(
            name="x", descriptor_type=DESCRIPTOR_MCP, custom_metadata={}, tags={},
            submit_for_approval=False,
        )
    )
    assert "customMetadata" not in stub.created[0]
    assert "tags" not in stub.created[0]


def test_create_passes_custom_metadata_and_tags():
    service, stub = _service()
    service.create_record(
        CreateRecordRequest(
            name="x",
            descriptor_type=DESCRIPTOR_MCP,
            custom_metadata={"team": "search", "humanInLoop": True},
            tags={"Platform": "bap"},
            submit_for_approval=False,
        )
    )
    assert stub.created[0]["customMetadata"] == {"team": "search", "humanInLoop": True}
    assert stub.created[0]["tags"] == {"Platform": "bap"}


SCHEMA_WITH_OWNER = {
    "customMetadataSchemaConfiguration": {
        "defaultSchema": json.dumps({"type": "object", "properties": {"owner": {"type": "string"}, "team": {"type": "string"}}})
    }
}


def test_create_stamps_the_caller_as_owner_over_the_clients_value():
    # The owner is who registered the record, not a form field: a client-sent
    # value is overwritten and the other fields are kept.
    service, stub = _service(registry=SCHEMA_WITH_OWNER)
    service.create_record(
        CreateRecordRequest(
            name="x", descriptor_type=DESCRIPTOR_MCP,
            custom_metadata={"team": "search", "owner": "spoofed"}, submit_for_approval=False,
        ),
        owner="admin@example.com",
    )
    assert stub.created[0]["customMetadata"] == {"team": "search", "owner": "admin@example.com"}


def test_create_stamps_owner_even_without_other_metadata():
    service, stub = _service(registry=SCHEMA_WITH_OWNER)
    service.create_record(
        CreateRecordRequest(name="x", descriptor_type=DESCRIPTOR_MCP, submit_for_approval=False),
        owner="admin@example.com",
    )
    assert stub.created[0]["customMetadata"] == {"owner": "admin@example.com"}


def test_create_skips_owner_when_the_schema_has_no_such_field():
    # A registry whose schema lacks `owner` (or has no schema) would reject the
    # create outright, so the stamp is dropped rather than failing registration.
    service, stub = _service(registry={})
    service.create_record(
        CreateRecordRequest(name="x", descriptor_type=DESCRIPTOR_MCP, submit_for_approval=False),
        owner="admin@example.com",
    )
    assert "customMetadata" not in stub.created[0]


def test_update_keeps_the_stored_owner_and_drops_the_clients():
    service, stub = _service(
        record={
            "customMetadata": {"owner": "creator@example.com", "tier": "internal"},
            "descriptors": {"mcpServer": {"data": json.dumps({"name": "bap/n", "description": "d", "version": "1.0.0"}), "dataSchemaVersion": "2025-12-11"}},
        }
    )
    service.update_record("r1", UpdateRecordRequest(custom_metadata={"tier": "partner", "owner": "thief@example.com"}))
    assert stub.updated[0]["customMetadata"] == {"optionalValue": {"tier": "partner", "owner": "creator@example.com"}}


def test_update_of_an_unowned_record_sends_no_owner():
    # Records registered before owner stamping existed have none; an edit does
    # not invent one.
    service, stub = _service(
        record={"descriptors": {"mcpServer": {"data": json.dumps({"name": "bap/n", "description": "d", "version": "1.0.0"}), "dataSchemaVersion": "2025-12-11"}}}
    )
    service.update_record("r1", UpdateRecordRequest(custom_metadata={"owner": "x", "tier": "partner"}))
    assert stub.updated[0]["customMetadata"] == {"optionalValue": {"tier": "partner"}}


def test_owner_identity_prefers_email_then_username_then_sub():
    from core.auth import AuthUser
    from services.registry_service import owner_identity

    assert owner_identity(AuthUser(sub="s", username="u", email="a@example.com")) == "a@example.com"
    assert owner_identity(AuthUser(sub="s", username="u")) == "u"
    assert owner_identity(AuthUser(sub="s", username="")) == "s"
    assert owner_identity(None) is None


def test_owner_identity_resolves_a_cognito_sub_through_the_directory():
    # A Cognito access token has no e-mail claim and its username is the sub;
    # the directory (ListUsers snapshot) turns it into the address.
    from core.auth import AuthUser
    from services.registry_service import owner_identity, set_owner_resolver

    try:
        set_owner_resolver(lambda sub: {"s-1": "me@example.com"}.get(sub))
        assert owner_identity(AuthUser(sub="s-1", username="s-1")) == "me@example.com"
        # Unknown to the directory: the sub itself, not an empty string.
        assert owner_identity(AuthUser(sub="s-2", username="s-2")) == "s-2"
        # The token's e-mail wins without a lookup.
        assert owner_identity(AuthUser(sub="s-1", username="u", email="t@example.com")) == "t@example.com"

        def boom(sub):
            raise RuntimeError("cognito down")
        set_owner_resolver(boom)
        assert owner_identity(AuthUser(sub="s-1", username="s-1")) == "s-1"
    finally:
        set_owner_resolver(None)


def test_update_wraps_custom_metadata_in_optional_value():
    service, stub = _service(
        record={"descriptors": {"mcpServer": {"data": json.dumps({"name": "bap/n", "description": "d", "version": "1.0.0"}), "dataSchemaVersion": "2025-12-11"}}}
    )
    service.update_record("r1", UpdateRecordRequest(custom_metadata={"tier": "partner"}))
    assert stub.updated[0]["customMetadata"] == {"optionalValue": {"tier": "partner"}}
    # Metadata alone touches no descriptor.
    assert "descriptors" not in stub.updated[0]


def test_update_of_an_http_descriptor_record_does_not_rebuild_descriptors():
    # An auto-detected runtime record has no agent card to merge an edit into,
    # and its descriptor is owned by the detector anyway.
    service, stub = _service(
        record={"recordType": "AGENT", "descriptors": {"http": {"source": {"fromUrl": {"url": INVOCATION_URL}}}}}
    )
    service.update_record("r1", UpdateRecordRequest(description="richer description"))
    assert stub.updated[0]["description"] == {"optionalValue": "richer description"}
    assert "descriptors" not in stub.updated[0]


def test_trigger_sync_sets_only_the_flag():
    service, stub = _service()
    service.trigger_sync("r1")
    assert stub.updated[0] == {"registryId": "reg-1", "recordId": "r1", "triggerSynchronization": True}


def test_info_parses_the_metadata_schema_per_record_type_and_the_mcp_endpoint():
    default = json.dumps({"type": "object", "properties": {"owner": {"type": "string"}}})
    override = json.dumps({"type": "object", "properties": {"tier": {"type": "string", "enum": ["internal", "public"]}}})
    service, _ = _service(
        registry={
            "registryId": "reg-1",
            "registryArn": "arn:aws:agent-registry:us-east-1:1:registry/reg-1",
            "name": "bap-registry",
            "status": "READY",
            "approvalConfiguration": {"autoApprovalRules": ["APPROVE_ALL"]},
            "customMetadataSchemaConfiguration": {
                "defaultSchema": default,
                "recordTypeSchemaOverrides": [{"recordType": "MCP", "schema": override}],
            },
            "autoDetection": {"configuration": {"scope": "ORGANIZATION", "enabled": True}, "status": "INACTIVE"},
            "encryptionConfiguration": {"kmsKeyArn": "arn:aws:kms:us-east-1:1:key/k"},
        }
    )
    info = service.get_registry_info()
    assert info.custom_metadata_schema == {"DEFAULT": json.loads(default), "MCP": json.loads(override)}
    assert info.auto_detection == {"enabled": True, "status": "INACTIVE"}
    assert info.kms_key_arn == "arn:aws:kms:us-east-1:1:key/k"
    assert info.mcp_endpoint == "https://agent-registry.us-east-1.api.aws/registry/reg-1/mcp"
    assert info.registry_arn.endswith("registry/reg-1")


def test_info_without_a_schema_reports_none():
    service, _ = _service(registry={"registryId": "reg-1", "status": "READY"})
    assert service.get_registry_info().custom_metadata_schema is None


def test_gateway_url_is_normalised_to_the_mcp_path():
    assert mcp_endpoint_of("https://g.example") == "https://g.example/mcp"
    assert mcp_endpoint_of("https://g.example/") == "https://g.example/mcp"
    assert mcp_endpoint_of("https://g.example/mcp") == "https://g.example/mcp"
    assert mcp_endpoint_of("https://g.example/mcp/") == "https://g.example/mcp"
    assert mcp_endpoint_of("https://g.example?x=1") == "https://g.example/mcp?x=1"
