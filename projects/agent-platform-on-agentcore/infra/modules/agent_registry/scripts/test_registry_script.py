import importlib.util, json, os, sys

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("registry_script", os.path.join(_here, "registry.py"))
registry_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry_script)


class FakeCtl:
    def __init__(self, existing=None):
        self.create_params = None
        self.update_params = []
        self.existing = existing
    def list_registries(self, **kw):
        return {"registries": [self.existing] if self.existing else []}
    def create_registry(self, **kw):
        self.create_params = kw
        return {"registryArn": "arn:aws:agent-registry:us-east-1:1:registry/reg-1"}
    def update_registry(self, **kw):
        self.update_params.append(kw)
        return {}
    def get_registry(self, **kw):
        return {"status": "READY"}


def test_control_service_is_agent_registry(monkeypatch):
    assert registry_script.CONTROL_SERVICE == "agent-registry-control"


def test_create_sends_discovery_and_autoapproval(monkeypatch):
    fake = FakeCtl()
    monkeypatch.setattr(registry_script, "client", lambda region: fake)
    args = registry_script.argparse.Namespace(
        name="proj-registry", region="us-east-1", description="d", auto_approval="true",
    )
    registry_script.cmd_create(args)
    assert fake.create_params["discoveryConfiguration"] == {"authorizerType": "AWS_IAM"}
    assert fake.create_params["approvalConfiguration"] == {"autoApprovalRules": ["APPROVE_ALL"]}
    assert "authorizerType" not in fake.create_params


def test_create_empty_rules_when_manual(monkeypatch):
    fake = FakeCtl()
    monkeypatch.setattr(registry_script, "client", lambda region: fake)
    args = registry_script.argparse.Namespace(
        name="proj-registry", region="us-east-1", description="", auto_approval="false",
    )
    registry_script.cmd_create(args)
    assert fake.create_params["approvalConfiguration"] == {"autoApprovalRules": []}


SCHEMA = '{"DEFAULT": "{\\"type\\":\\"object\\",\\"properties\\":{\\"owner\\":{\\"type\\":\\"string\\"}}}", "MCP": "{\\"type\\":\\"object\\",\\"properties\\":{}}"}'


def _args(**overrides):
    base = dict(
        name="proj-registry", region="us-east-1", description="", auto_approval="true",
        kms_key_arn="", custom_metadata_schema_json="{}",
    )
    base.update(overrides)
    return registry_script.argparse.Namespace(**base)


def test_schema_config_splits_default_from_overrides():
    config = registry_script.schema_config(SCHEMA)
    assert config["defaultSchema"].startswith('{"type":"object"')
    assert config["recordTypeSchemaOverrides"] == [
        {"recordType": "MCP", "schema": '{"type":"object","properties":{}}'}
    ]
    assert registry_script.schema_config("{}") == {}


def test_create_sends_schema_and_kms_key(monkeypatch):
    fake = FakeCtl()
    monkeypatch.setattr(registry_script, "client", lambda region: fake)
    registry_script.cmd_create(
        _args(kms_key_arn="arn:aws:kms:us-east-1:1:key/k", custom_metadata_schema_json=SCHEMA)
    )
    assert fake.create_params["encryptionConfiguration"] == {"kmsKeyArn": "arn:aws:kms:us-east-1:1:key/k"}
    assert "defaultSchema" in fake.create_params["customMetadataSchemaConfiguration"]


def test_create_omits_schema_and_kms_when_empty(monkeypatch):
    fake = FakeCtl()
    monkeypatch.setattr(registry_script, "client", lambda region: fake)
    registry_script.cmd_create(_args())
    assert "encryptionConfiguration" not in fake.create_params
    assert "customMetadataSchemaConfiguration" not in fake.create_params


def test_adopt_resyncs_schema_as_optional_value(monkeypatch):
    fake = FakeCtl(existing={"registryId": "reg-1", "name": "proj-registry", "status": "READY"})
    monkeypatch.setattr(registry_script, "client", lambda region: fake)
    registry_script.cmd_create(_args(custom_metadata_schema_json=SCHEMA))
    schema_updates = [u for u in fake.update_params if "customMetadataSchemaConfiguration" in u]
    assert schema_updates[0]["customMetadataSchemaConfiguration"]["optionalValue"]["defaultSchema"]
    assert fake.create_params is None


def test_adopt_reports_but_survives_a_non_additive_schema_change(monkeypatch, capsys):
    class Rejecting(FakeCtl):
        def update_registry(self, **kw):
            if "customMetadataSchemaConfiguration" in kw:
                raise registry_script.ClientError(
                    {"Error": {"Code": "ValidationException", "Message": "cannot remove field"}},
                    "UpdateRegistry",
                )
            return super().update_registry(**kw)

    fake = Rejecting(existing={"registryId": "reg-1", "name": "proj-registry", "status": "READY"})
    monkeypatch.setattr(registry_script, "client", lambda region: fake)
    registry_script.cmd_create(_args(custom_metadata_schema_json=SCHEMA))
    assert "additive" in capsys.readouterr().err


def test_merge_keeps_saved_fields_and_enum_values_the_default_no_longer_lists():
    # docs_url was retired from the module default on 2026-10-10; a registry
    # that saved it must still be sent it, or AWS rejects the update.
    live = {
        "defaultSchema": json.dumps({"type": "object", "properties": {
            "owner": {"type": "string"},
            "tier": {"type": "string", "enum": ["internal", "legacy"]},
            "docs_url": {"type": "string", "format": "uri"},
        }, "required": ["owner"]}),
        "recordTypeSchemaOverrides": [{"recordType": "MCP", "schema": json.dumps({"type": "object", "properties": {"x": {"type": "boolean"}}})}],
    }
    desired = {
        "defaultSchema": json.dumps({"type": "object", "properties": {
            "owner": {"type": "string"},
            "team": {"type": "string"},
            "tier": {"type": "string", "enum": ["internal", "partner"]},
        }}),
    }
    merged = registry_script.merge_schema_config(live, desired)
    default = json.loads(merged["defaultSchema"])
    assert set(default["properties"]) == {"owner", "tier", "docs_url", "team"}
    assert default["properties"]["tier"]["enum"] == ["internal", "partner", "legacy"]
    assert "required" not in default  # required-ness may be given up
    assert merged["recordTypeSchemaOverrides"] == live["recordTypeSchemaOverrides"]


def test_adopt_merges_the_live_schema_before_updating(monkeypatch):
    class WithSchema(FakeCtl):
        def get_registry(self, **kw):
            return {"status": "READY", "customMetadataSchemaConfiguration": {
                "defaultSchema": json.dumps({"type": "object", "properties": {"docs_url": {"type": "string", "format": "uri"}}})
            }}

    fake = WithSchema(existing={"registryId": "reg-1", "name": "proj-registry", "status": "READY"})
    monkeypatch.setattr(registry_script, "client", lambda region: fake)
    registry_script.cmd_create(_args(custom_metadata_schema_json=SCHEMA))
    sent = [u for u in fake.update_params if "customMetadataSchemaConfiguration" in u][0]
    default = json.loads(sent["customMetadataSchemaConfiguration"]["optionalValue"]["defaultSchema"])
    assert set(default["properties"]) == {"owner", "docs_url"}
