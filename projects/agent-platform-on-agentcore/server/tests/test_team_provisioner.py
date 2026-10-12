"""Creating a team is three AWS resources, each step idempotent and each failure named."""
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.team import TeamConfig  # noqa: E402
from services.team_provisioner import (  # noqa: E402
    ProvisionError, TeamProvisioner, account_id_of, cedar_statement, model_arns_for,
    policy_name, role_arn, role_name, role_path, role_policy_document,
)

ACCOUNT = "123456789012"
GATEWAY = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/bap-gateway-abc"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/bap-harness-boundary"


def err(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class Cognito:
    def __init__(self, groups=None):
        self.groups = dict(groups or {})
        self.calls = []

    def create_group(self, UserPoolId, GroupName, Description=""):
        self.calls.append(("create_group", GroupName))
        if GroupName in self.groups:
            raise err("GroupExistsException")
        self.groups[GroupName] = Description

    def get_group(self, UserPoolId, GroupName):
        if GroupName not in self.groups:
            raise err("ResourceNotFoundException")
        return {"Group": {"GroupName": GroupName, "Description": self.groups[GroupName]}}

    def update_group(self, UserPoolId, GroupName, Description=""):
        self.calls.append(("update_group", GroupName))
        self.groups[GroupName] = Description

    def delete_group(self, UserPoolId, GroupName):
        self.calls.append(("delete_group", GroupName))
        if GroupName not in self.groups:
            raise err("ResourceNotFoundException")
        del self.groups[GroupName]


class Iam:
    def __init__(self, roles=None, fail_create=None):
        self.roles = dict(roles or {})
        self.inline = {}
        self.fail_create = fail_create
        self.calls = []

    def create_role(self, **kw):
        self.calls.append(("create_role", kw["RoleName"], kw["Path"], kw.get("PermissionsBoundary")))
        if self.fail_create:
            raise err(self.fail_create)
        if kw["RoleName"] in self.roles:
            raise err("EntityAlreadyExists")
        self.roles[kw["RoleName"]] = kw
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role{kw['Path']}{kw['RoleName']}"}}

    def get_role(self, RoleName):
        if RoleName not in self.roles:
            raise err("NoSuchEntity")
        kw = self.roles[RoleName]
        role = {"Arn": f"arn:aws:iam::{ACCOUNT}:role{kw['Path']}{RoleName}", "Path": kw["Path"]}
        if kw.get("PermissionsBoundary"):
            role["PermissionsBoundary"] = {"PermissionsBoundaryArn": kw["PermissionsBoundary"]}
        return {"Role": role}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.calls.append(("put_role_policy", RoleName))
        self.inline[RoleName] = json.loads(PolicyDocument)

    def tag_role(self, RoleName, Tags):
        self.calls.append(("tag_role", RoleName))

    def list_role_policies(self, RoleName):
        return {"PolicyNames": [f"{RoleName}-policy"] if RoleName in self.inline else []}

    def delete_role_policy(self, RoleName, PolicyName):
        self.calls.append(("delete_role_policy", RoleName))
        self.inline.pop(RoleName, None)

    def delete_role(self, RoleName):
        self.calls.append(("delete_role", RoleName))
        if RoleName not in self.roles:
            raise err("NoSuchEntity")
        del self.roles[RoleName]


class AgentCore:
    def __init__(self):
        self.policies = {}
        self.calls = []
        self.n = 0

    def create_policy(self, **kw):
        if any(p["name"] == kw["name"] for p in self.policies.values()):
            raise err("ConflictException")
        self.n += 1
        pid = f"pol-{self.n}"
        self.policies[pid] = kw
        self.calls.append(("create_policy", kw["name"]))
        return {"policyId": pid, "name": kw["name"]}

    def update_policy(self, **kw):
        self.calls.append(("update_policy", kw["policyId"]))
        self.policies[kw["policyId"]].update(kw)
        return {"policyId": kw["policyId"]}

    def list_policies(self, policyEngineId, nextToken=None):
        items = [{"policyId": i, "name": p["name"]} for i, p in self.policies.items()]
        if nextToken is None and len(items) > 1:  # two pages, to exercise paging
            return {"policies": items[:1], "nextToken": "t2"}
        return {"policies": items[1:] if nextToken else items}

    def get_policy(self, policyEngineId, policyId):
        if policyId not in self.policies:
            raise err("ResourceNotFoundException")
        return {"policyId": policyId}

    def delete_policy(self, policyEngineId, policyId):
        self.calls.append(("delete_policy", policyId))
        self.policies.pop(policyId, None)
        return {}


def provisioner(cognito=None, iam=None, agentcore=None, *, engine="eng-1", gateway=GATEWAY, shared=("bap-web-search___WebSearch",)):
    return TeamProvisioner(
        cognito or Cognito(), iam or Iam(), agentcore or AgentCore(),
        user_pool_id="pool-1", project="bap", region="us-east-1", account_id=ACCOUNT,
        boundary_arn=BOUNDARY, skills_bucket="bap-skills", policy_engine_id=engine, gateway_arn=gateway,
        shared_actions=list(shared), propagation_seconds=10, sleep=lambda s: None,
    )


def team(**kw):
    base = dict(name="fin-ops", label="재무운영", allowed_tools=["bap-platform-tools___fetch_url", "bap-platform-tools___calculate"],
                managed_by="platform", status="provisioning")
    base.update(kw)
    return TeamConfig(**base)


def test_names_follow_the_platform_conventions():
    assert role_name("bap", "fin-ops") == "bap-harness-fin-ops"
    assert role_path("bap") == "/bap/teams/"
    assert role_arn(ACCOUNT, "bap", "fin-ops") == f"arn:aws:iam::{ACCOUNT}:role/bap/teams/bap-harness-fin-ops"
    assert policy_name("bap", "fin-ops", "tools") == "bap_team_fin_ops_tools"
    assert policy_name("bap", "x" * 32, "shared") == "bap_team_" + "x" * 32 + "_shared"
    assert len(policy_name("bap", "x" * 32, "shared")) <= 48
    assert account_id_of(BOUNDARY) == ACCOUNT


def test_cedar_condition_is_exact_arn_or_session_suffix():
    stmt = cedar_statement(GATEWAY, ACCOUNT, "bap-harness-fin", ["bap-platform-tools___fetch_url"])
    sts = f"arn:aws:sts::{ACCOUNT}:assumed-role/bap-harness-fin"
    assert f'principal.id == "{sts}"' in stmt
    assert f'principal.id like "{sts}/*"' in stmt
    assert f'principal.id like "{sts}*"' not in stmt  # would also match bap-harness-finance
    assert 'action in [AgentCore::Action::"bap-platform-tools___fetch_url"]' in stmt
    assert f'resource == AgentCore::Gateway::"{GATEWAY}"' in stmt
    assert stmt.startswith("permit(principal is AgentCore::IamEntity,")
    assert stmt.rstrip().endswith(";")


def test_role_policy_narrows_models_and_mirrors_the_harness_role():
    arns = model_arns_for(["global.anthropic.claude-sonnet-5-5"], "us-east-1", ACCOUNT)
    assert f"arn:aws:bedrock:us-east-1:{ACCOUNT}:inference-profile/global.anthropic.claude-sonnet-5-5" in arns
    assert "arn:aws:bedrock:*::foundation-model/*" in arns
    assert model_arns_for([], "us-east-1", ACCOUNT) == ["*"]
    doc = role_policy_document(["*"], "bap-skills")
    sids = {s["Sid"] for s in doc["Statement"]}
    assert {"BedrockModelInvocation", "ContainerImagePull", "Observability", "WorkloadIdentity",
            "BuiltInTools", "InvokeGateway", "AgentCoreMemory", "SkillsBucketRead"} <= sids
    skills = next(s for s in doc["Statement"] if s["Sid"] == "SkillsBucketRead")
    assert skills["Resource"] == ["arn:aws:s3:::bap-skills", "arn:aws:s3:::bap-skills/*"]
    built_in = next(s for s in doc["Statement"] if s["Sid"] == "BuiltInTools")
    assert "bedrock-agentcore:ConnectBrowserLiveViewStream" not in built_in["Action"]


def test_provision_creates_group_role_policies_and_fills_the_team():
    cognito, iam, ac = Cognito(), Iam(), AgentCore()
    out = provisioner(cognito, iam, ac).provision(team())
    assert cognito.groups["team:fin-ops"] == "재무운영"
    assert ("create_role", "bap-harness-fin-ops", "/bap/teams/", BOUNDARY) in iam.calls
    assert out.execution_role_arn == role_arn(ACCOUNT, "bap", "fin-ops")
    assert set(out.policy_ids) == {"tools", "shared"}
    names = {p["name"] for p in ac.policies.values()}
    assert names == {"bap_team_fin_ops_tools", "bap_team_fin_ops_shared"}
    tools_pol = next(p for p in ac.policies.values() if p["name"].endswith("_tools"))
    assert tools_pol["policyEngineId"] == "eng-1"
    assert tools_pol["validationMode"] == "FAIL_ON_ANY_FINDINGS" and tools_pol["enforcementMode"] == "ACTIVE"
    assert "bap-platform-tools___calculate" in tools_pol["definition"]["cedar"]["statement"]
    assert out.status == "provisioning"  # the caller marks ready


def test_create_group_passes_when_it_already_exists():
    cognito = Cognito({"team:fin-ops": "old"})
    iam = Iam()
    out = provisioner(cognito, iam).provision(team())
    assert out.execution_role_arn
    assert ("update_group", "team:fin-ops") in cognito.calls  # label refreshed, not an error


def test_existing_role_is_reused_not_an_error():
    iam = Iam()
    first = provisioner(iam=iam).provision(team())
    again = provisioner(iam=iam).provision(first)
    assert again.execution_role_arn == first.execution_role_arn
    assert iam.calls.count(("create_role", "bap-harness-fin-ops", "/bap/teams/", BOUNDARY)) == 2
    assert len(iam.roles) == 1


def test_iam_failure_names_the_step_and_leaves_the_group():
    cognito = Cognito()
    with pytest.raises(ProvisionError) as caught:
        provisioner(cognito, Iam(fail_create="AccessDenied")).provision(team())
    assert caught.value.step == "iam_role" and "AccessDenied" in caught.value.message
    assert "team:fin-ops" in cognito.groups  # retry resumes from the role step


def test_without_engine_no_policies_and_empty_policy_ids():
    ac = AgentCore()
    out = provisioner(agentcore=ac, engine="").provision(team())
    assert out.policy_ids == {} and ac.calls == []


def test_empty_tool_list_skips_the_tools_policy_but_keeps_shared():
    ac = AgentCore()
    out = provisioner(agentcore=ac).provision(team(allowed_tools=[]))
    assert set(out.policy_ids) == {"shared"}


def test_role_name_with_trailing_newline_is_refused():
    with pytest.raises(ValueError):
        cedar_statement(GATEWAY, ACCOUNT, "bap-harness-fin\n", ["a___b"])


def test_pattern_entries_never_reach_cedar():
    ac = AgentCore()
    out = provisioner(agentcore=ac).provision(team(allowed_tools=["@builtin", "*"]))
    assert set(out.policy_ids) == {"shared"}
    out = provisioner(agentcore=ac).provision(team(allowed_tools=["@builtin", "bap-platform-tools___calculate"]))
    statement = ac.policies[out.policy_ids["tools"]]["definition"]["cedar"]["statement"]
    assert "bap-platform-tools___calculate" in statement and "@builtin" not in statement


def test_apply_settings_updates_policy_role_doc_and_group_label():
    cognito, iam, ac = Cognito(), Iam(), AgentCore()
    p = provisioner(cognito, iam, ac)
    t = p.provision(team())
    t = t.model_copy(update={"label": "재무", "allowed_models": ["m1"], "allowed_tools": ["bap-platform-tools___calculate"]})
    out = p.apply_settings(t)
    assert ("update_policy", t.policy_ids["tools"]) in ac.calls
    assert "fetch_url" not in ac.policies[t.policy_ids["tools"]]["definition"]["cedar"]["statement"]
    assert iam.inline["bap-harness-fin-ops"]["Statement"][0]["Resource"][0].endswith("inference-profile/m1")
    assert cognito.groups["team:fin-ops"] == "재무"
    assert out.policy_ids == t.policy_ids
    # Tools emptied → tools policy deleted; tools added back → recreated.
    out = p.apply_settings(out.model_copy(update={"allowed_tools": []}))
    assert "tools" not in out.policy_ids
    out = p.apply_settings(out.model_copy(update={"allowed_tools": ["bap-platform-tools___calculate"]}))
    assert "tools" in out.policy_ids


def test_deprovision_runs_in_reverse_and_tolerates_missing_pieces():
    cognito, iam, ac = Cognito(), Iam(), AgentCore()
    p = provisioner(cognito, iam, ac)
    t = p.provision(team())
    p.deprovision(t)
    assert ac.policies == {} and iam.roles == {} and "team:fin-ops" not in cognito.groups
    order = [c[0] for c in ac.calls + iam.calls + cognito.calls if c[0].startswith("delete")]
    assert order.index("delete_policy") < order.index("delete_role") < order.index("delete_group")
    p.deprovision(t)  # nothing left: still no exception


def test_probes_report_existence():
    cognito, iam, ac = Cognito(), Iam(), AgentCore()
    p = provisioner(cognito, iam, ac)
    t = p.provision(team())
    assert p.group_exists("fin-ops") and p.role_exists(t) and p.policies_exist(t) == {"tools": True, "shared": True}
    assert not p.group_exists("ghost") and not p.role_exists(team(name="ghost"))


def test_propagation_wait_is_part_of_provision():
    slept = []
    p = TeamProvisioner(Cognito(), Iam(), AgentCore(), user_pool_id="pool-1", project="bap", region="us-east-1",
                        account_id=ACCOUNT, boundary_arn=BOUNDARY, skills_bucket="bap-skills", policy_engine_id="",
                        gateway_arn="", shared_actions=[], propagation_seconds=7, sleep=slept.append)
    p.provision(team())
    assert slept == [7]


def test_disabled_without_boundary_or_pool():
    p = TeamProvisioner(Cognito(), Iam(), AgentCore(), user_pool_id="", project="bap", region="us-east-1",
                        account_id=ACCOUNT, boundary_arn="", skills_bucket="", policy_engine_id="", gateway_arn="",
                        shared_actions=[])
    assert p.enabled is False


def test_cedar_statement_rejects_unsafe_action_and_role_names():
    for bad in ['x"', "a\\b", "tool*", "a\nb", 'x"]); //', "", "a___b\n"]:
        with pytest.raises(ValueError):
            cedar_statement(GATEWAY, ACCOUNT, "bap-harness-fin", [bad])
        with pytest.raises(ValueError):
            cedar_statement(GATEWAY, ACCOUNT, bad, ["bap-platform-tools___fetch_url"])
    with pytest.raises(ValueError):
        cedar_statement('arn"x', ACCOUNT, "bap-harness-fin", ["a___b"])
    assert "a-b___c.d_e" in cedar_statement(GATEWAY, ACCOUNT, "bap-harness-fin", ["a-b___c.d_e"])


def test_unsafe_tool_name_becomes_a_cedar_policy_provision_error():
    with pytest.raises(ProvisionError) as caught:
        provisioner().provision(team(allowed_tools=['x"]); //']))
    assert caught.value.step == "cedar_policy"


def test_retry_adopts_existing_policy_by_name():
    ac = AgentCore()
    p = provisioner(agentcore=ac)
    first = p.provision(team())
    again = p.provision(first.model_copy(update={"policy_ids": {}}))
    assert again.policy_ids == first.policy_ids
    assert len(ac.policies) == 2
    assert ("update_policy", first.policy_ids["tools"]) in ac.calls


def test_existing_role_outside_platform_path_is_refused():
    foreign = {"bap-harness-fin-ops": {"Path": "/", "RoleName": "bap-harness-fin-ops"}}
    iam = Iam(roles=foreign)
    with pytest.raises(ProvisionError) as caught:
        provisioner(iam=iam).provision(team())
    assert caught.value.step == "iam_role" and "outside platform management" in caught.value.message
    assert not any(c[0] == "put_role_policy" for c in iam.calls)
    with pytest.raises(ProvisionError):
        provisioner(iam=iam).deprovision(team())
    assert "bap-harness-fin-ops" in iam.roles  # left alone
