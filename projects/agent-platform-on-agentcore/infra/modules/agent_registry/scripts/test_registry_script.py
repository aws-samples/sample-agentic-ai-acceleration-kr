import importlib.util, os, sys

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("registry_script", os.path.join(_here, "registry.py"))
registry_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry_script)


class FakeCtl:
    def __init__(self):
        self.create_params = None
    def list_registries(self, **kw):
        return {"registries": []}
    def create_registry(self, **kw):
        self.create_params = kw
        return {"registryArn": "arn:aws:agent-registry:us-east-1:1:registry/reg-1"}
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
