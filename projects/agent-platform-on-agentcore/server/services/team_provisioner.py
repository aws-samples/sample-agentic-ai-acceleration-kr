"""The three AWS resources a team is, created at runtime from the Organization page.

A team is a Cognito group `team:<name>` (membership), a harness execution role
(`<project>-harness-<name>`, what the team's harnesses run as, and the principal
the gateway policy engine tells teams apart by) and Cedar policies permitting
that role the team's tools. This module creates, updates and deletes those and
nothing else; every boto3 call for team provisioning lives here.

Fences (mirrored in infra/modules/iam): the role is created under the path
`/<project>/teams/` with the harness boundary policy attached, so the server can
never make a role wider than the default harness role. The inline policy is the
harness_role module's document rendered in Python; the Bedrock statement narrows
to the team's models when it has any.

Every step is idempotent — an existing group, role or policy is reused — and a
failure raises ProvisionError naming the step, so the team row records where it
stopped and a retry resumes there. Order matters on delete: policies first (they
name the role), then the role, then the group.

Assumed-role ARNs drop the path (`arn:aws:sts::<acct>:assumed-role/<name>/<sess>`),
which is why the Cedar condition is written from the role *name*, exactly as the
platform policies in infra/modules/gateway_policies do.
"""
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from botocore.exceptions import ClientError

from models.team import TeamConfig, cedar_actions

logger = logging.getLogger(__name__)

GROUP_PREFIX = "team:"
STEP_GROUP, STEP_ROLE, STEP_POLICY, STEP_PROPAGATION = "cognito_group", "iam_role", "cedar_policy", "propagation"
_EXISTS = {"EntityAlreadyExists", "GroupExistsException", "ConflictException"}
_MISSING = {"NoSuchEntity", "ResourceNotFoundException"}
# Cedar string literals are built by interpolation, so anything that reaches one
# must be provably inert: a `"` or `\\` in a tool name could close the literal and
# comment out the `when` clause, turning a team policy into permit-everyone.
# Gateway action names are `<target>___<tool>`.
# fullmatch, not match: `$` also accepts a trailing "\n".
SAFE_ACTION_RE = re.compile(r"[A-Za-z0-9_.-]+(___[A-Za-z0-9_.-]+)?")
SAFE_ROLE_RE = re.compile(r"[A-Za-z0-9+=,.@_-]+")


class ProvisionError(Exception):
    def __init__(self, step: str, message: str):
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


# ── pure naming / document helpers ─────────────────────────────────────────
def role_name(project: str, team: str) -> str:
    return f"{project}-harness-{team}"


def role_path(project: str) -> str:
    return f"/{project}/teams/"


def role_arn(account_id: str, project: str, team: str) -> str:
    return f"arn:aws:iam::{account_id}:role{role_path(project)}{role_name(project, team)}"


def policy_name(project: str, team: str, kind: str) -> str:
    """AgentCore policy names: [A-Za-z][A-Za-z0-9_]*, max 48. 3+6+32+7 = 48 worst case."""
    return f"{project.replace('-', '_')}_team_{team.replace('-', '_')}_{kind}"


def account_id_of(arn: str) -> str:
    parts = arn.split(":")
    return parts[4] if len(parts) > 4 else ""


def assume_role_document(region: str, account_id: str) -> Dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"},
            },
        }],
    }


def model_arns_for(model_ids: List[str], region: str, account_id: str) -> List[str]:
    """IAM resources for the team's models. Inference profiles are account
    resources; the underlying foundation models must be invocable in every region
    a global profile routes to, which only a wildcard covers. The server-side
    allow-list (TeamService.allowed_models_for) remains the precise check."""
    ids = [m for m in model_ids if isinstance(m, str) and m]
    if not ids:
        return ["*"]
    arns = [f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{m}" for m in ids]
    arns.append("arn:aws:bedrock:*::foundation-model/*")
    return arns


def role_policy_document(model_arns: List[str], skills_bucket: str) -> Dict[str, Any]:
    """infra/modules/harness_role/main.tf, statement for statement."""
    skills = [f"arn:aws:s3:::{skills_bucket}", f"arn:aws:s3:::{skills_bucket}/*"] if skills_bucket else []
    statements: List[Dict[str, Any]] = [
        {"Sid": "BedrockModelInvocation", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": model_arns},
        {"Sid": "ContainerImagePull", "Effect": "Allow",
         "Action": ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"], "Resource": "*"},
        {"Sid": "Observability", "Effect": "Allow", "Action": [
            "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups",
            "logs:DescribeLogStreams", "xray:PutTraceSegments", "xray:PutTelemetryRecords",
            "xray:GetSamplingRules", "xray:GetSamplingTargets", "cloudwatch:PutMetricData"], "Resource": "*"},
        {"Sid": "WorkloadIdentity", "Effect": "Allow", "Action": [
            "bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT"], "Resource": "*"},
        # ConnectBrowserLiveViewStream deliberately absent (see the terraform module).
        {"Sid": "BuiltInTools", "Effect": "Allow", "Action": [
            "bedrock-agentcore:StartBrowserSession", "bedrock-agentcore:StopBrowserSession",
            "bedrock-agentcore:GetBrowserSession", "bedrock-agentcore:ListBrowserSessions",
            "bedrock-agentcore:ConnectBrowserAutomationStream", "bedrock-agentcore:StartCodeInterpreterSession",
            "bedrock-agentcore:StopCodeInterpreterSession", "bedrock-agentcore:GetCodeInterpreterSession",
            "bedrock-agentcore:ListCodeInterpreterSessions", "bedrock-agentcore:InvokeCodeInterpreter"], "Resource": "*"},
        {"Sid": "InvokeGateway", "Effect": "Allow", "Action": ["bedrock-agentcore:InvokeGateway"], "Resource": "*"},
        {"Sid": "AgentCoreMemory", "Effect": "Allow", "Action": [
            "bedrock-agentcore:CreateEvent", "bedrock-agentcore:GetEvent", "bedrock-agentcore:ListEvents",
            "bedrock-agentcore:ListSessions", "bedrock-agentcore:RetrieveMemoryRecords",
            "bedrock-agentcore:GetMemoryRecord", "bedrock-agentcore:ListMemoryRecords"], "Resource": "*"},
    ]
    if skills:
        statements.append({"Sid": "SkillsBucketRead", "Effect": "Allow",
                           "Action": ["s3:GetObject", "s3:ListBucket"], "Resource": skills})
    return {"Version": "2012-10-17", "Statement": statements}


def cedar_statement(gateway_arn: str, account_id: str, role_name_: str, actions: List[str]) -> str:
    """Same shape as infra/modules/gateway_policies: typed principal (an untyped
    one also covers UnauthenticatedUser, which has no `id` and fails validation),
    exact ARN or ARN + "/<session>" — never a bare prefix wildcard."""
    if not SAFE_ROLE_RE.fullmatch(role_name_ or ""):
        raise ValueError(f"unsafe role name for a Cedar policy: {role_name_!r}")
    for a in actions:
        if not SAFE_ACTION_RE.fullmatch(a or ""):
            raise ValueError(f"unsafe action name for a Cedar policy: {a!r}")
    if not gateway_arn or '"' in gateway_arn or "\\" in gateway_arn or "\n" in gateway_arn:
        raise ValueError("unsafe gateway ARN for a Cedar policy")
    sts = f"arn:aws:sts::{account_id}:assumed-role/{role_name_}"
    cond = f'(principal.id == "{sts}" || principal.id like "{sts}/*")'
    action_list = ", ".join(f'AgentCore::Action::"{a}"' for a in actions)
    return (f"permit(principal is AgentCore::IamEntity, action in [{action_list}], "
            f'resource == AgentCore::Gateway::"{gateway_arn}") when {{ {cond} }};')


# ── the provisioner ────────────────────────────────────────────────────────
class TeamProvisioner:
    def __init__(self, cognito, iam, agentcore, *, user_pool_id: str, project: str, region: str,
                 account_id: str, boundary_arn: str, skills_bucket: str, policy_engine_id: str,
                 gateway_arn: str, shared_actions: List[str], propagation_seconds: int = 10,
                 sleep: Callable[[float], None] = time.sleep):
        self._cognito, self._iam, self._agentcore = cognito, iam, agentcore
        self._pool = user_pool_id
        self._project, self._region, self._account = project, region, account_id
        self._boundary, self._skills = boundary_arn, skills_bucket
        self._engine, self._gateway = policy_engine_id, gateway_arn
        self._shared = [a for a in shared_actions if a]
        self._wait, self._sleep = propagation_seconds, sleep

    @property
    def enabled(self) -> bool:
        return bool(self._boundary and self._pool and self._account)

    @property
    def policies_enabled(self) -> bool:
        return bool(self._engine and self._gateway)

    # ── steps ──────────────────────────────────────────────────────────────
    def _group(self, team: TeamConfig) -> None:
        name = f"{GROUP_PREFIX}{team.name}"
        try:
            self._cognito.create_group(UserPoolId=self._pool, GroupName=name, Description=team.label or team.name)
        except ClientError as exc:
            if _code(exc) not in _EXISTS:
                raise ProvisionError(STEP_GROUP, f"{_code(exc)}: {exc}") from exc
            try:
                self._cognito.update_group(UserPoolId=self._pool, GroupName=name, Description=team.label or team.name)
            except ClientError as exc2:
                raise ProvisionError(STEP_GROUP, f"{_code(exc2)}: {exc2}") from exc2

    def _role(self, team: TeamConfig) -> str:
        name = role_name(self._project, team.name)
        arn = ""
        try:
            created = self._iam.create_role(
                RoleName=name, Path=role_path(self._project), PermissionsBoundary=self._boundary,
                AssumeRolePolicyDocument=json.dumps(assume_role_document(self._region, self._account)),
                Description=f"Harness execution role of team {team.name} (Organization page)",
                Tags=[{"Key": "Platform", "Value": self._project}, {"Key": "Team", "Value": team.name},
                      {"Key": "ManagedBy", "Value": "platform"}],
            )
            arn = str((created or {}).get("Role", {}).get("Arn", ""))
        except ClientError as exc:
            if _code(exc) not in _EXISTS:
                raise ProvisionError(STEP_ROLE, f"{_code(exc)}: {exc}") from exc
        if not arn:
            # Same name already exists: adopt it only if it is one of ours.
            arn = self._platform_role_arn(name)
        self._role_policy(team)
        return arn or role_arn(self._account, self._project, team.name)

    def _platform_role_arn(self, name: str) -> str:
        """ARN of the role `name` if it sits under our path with our boundary,
        else ProvisionError. A same-named role created by hand must never be
        rewritten or deleted by the Organization page."""
        try:
            role = self._iam.get_role(RoleName=name).get("Role", {})
        except ClientError as exc:
            raise ProvisionError(STEP_ROLE, f"{_code(exc)}: {exc}") from exc
        boundary = (role.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
        if role.get("Path") != role_path(self._project) or boundary != self._boundary:
            raise ProvisionError(STEP_ROLE, f"role {name} exists outside platform management")
        return str(role.get("Arn", ""))

    def _role_policy(self, team: TeamConfig) -> None:
        name = role_name(self._project, team.name)
        doc = role_policy_document(model_arns_for(team.allowed_models, self._region, self._account), self._skills)
        try:
            self._iam.put_role_policy(RoleName=name, PolicyName=f"{name}-policy", PolicyDocument=json.dumps(doc))
        except ClientError as exc:
            raise ProvisionError(STEP_ROLE, f"{_code(exc)}: {exc}") from exc

    def _policies(self, team: TeamConfig) -> Dict[str, str]:
        """Create or update the team's Cedar policies; returns the new policy_ids."""
        if not self.policies_enabled:
            return {}
        rn = role_name(self._project, team.name)
        wanted = {}
        # Harness patterns (`@builtin`, `*`) are not gateway actions; a team
        # with only patterns has no tools policy.
        actions = cedar_actions(team.allowed_tools)
        if actions:
            wanted["tools"] = actions
        if self._shared:
            wanted["shared"] = self._shared
        ids = dict(team.policy_ids)
        try:
            for kind in ("tools", "shared"):
                actions = wanted.get(kind)
                existing = ids.get(kind)
                if actions is None:
                    if existing:
                        self._delete_policy(existing)
                        ids.pop(kind, None)
                    continue
                statement = cedar_statement(self._gateway, self._account, rn, actions)
                if existing and self._policy_exists(existing):
                    self._agentcore.update_policy(policyEngineId=self._engine, policyId=existing,
                                                  definition={"cedar": {"statement": statement}},
                                                  validationMode="FAIL_ON_ANY_FINDINGS")
                else:
                    ids[kind] = self._create_or_adopt(team.name, kind, statement)
        except ValueError as exc:
            raise ProvisionError(STEP_POLICY, str(exc)) from exc
        except ClientError as exc:
            raise ProvisionError(STEP_POLICY, f"{_code(exc)}: {exc}") from exc
        return ids

    def _create_or_adopt(self, team_name: str, kind: str, statement: str) -> str:
        """Create the policy; if a previous attempt already did (its id lost
        with a failed row write), adopt it by name and bring it up to date."""
        name = policy_name(self._project, team_name, kind)
        definition = {"cedar": {"statement": statement}}
        try:
            resp = self._agentcore.create_policy(
                policyEngineId=self._engine, name=name, description=f"Team {team_name}: {kind}",
                definition=definition, validationMode="FAIL_ON_ANY_FINDINGS", enforcementMode="ACTIVE")
            return str(resp["policyId"])
        except ClientError as exc:
            if _code(exc) not in _EXISTS:
                raise
        found = self._find_policy_id(name)
        if not found:
            raise ProvisionError(STEP_POLICY, f"policy {name} already exists but could not be found in the engine")
        self._agentcore.update_policy(policyEngineId=self._engine, policyId=found, definition=definition,
                                      validationMode="FAIL_ON_ANY_FINDINGS")
        return found

    def _find_policy_id(self, name: str) -> Optional[str]:
        """ListPolicies pages with nextToken; match on the exact name."""
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"policyEngineId": self._engine}
            if token:
                kwargs["nextToken"] = token
            resp = self._agentcore.list_policies(**kwargs)
            for pol in resp.get("policies", []):
                if pol.get("name") == name:
                    return str(pol["policyId"])
            token = resp.get("nextToken")
            if not token:
                return None

    def _policy_exists(self, policy_id: str) -> bool:
        try:
            self._agentcore.get_policy(policyEngineId=self._engine, policyId=policy_id)
            return True
        except ClientError as exc:
            if _code(exc) in _MISSING:
                return False
            raise

    def _delete_policy(self, policy_id: str) -> None:
        try:
            self._agentcore.delete_policy(policyEngineId=self._engine, policyId=policy_id)
        except ClientError as exc:
            if _code(exc) not in _MISSING:
                raise

    # ── public ─────────────────────────────────────────────────────────────
    def provision(self, team: TeamConfig) -> TeamConfig:
        """Group → role → policies → wait. Returns the team with role ARN and
        policy ids; status is left to the caller (it also owns the row)."""
        self._group(team)
        arn = self._role(team)
        team = team.model_copy(update={"execution_role_arn": arn})
        team = team.model_copy(update={"policy_ids": self._policies(team)})
        try:
            self._iam.get_role(RoleName=role_name(self._project, team.name))
        except ClientError as exc:
            raise ProvisionError(STEP_PROPAGATION, f"{_code(exc)}: {exc}") from exc
        # IAM is eventually consistent; AgentCore may refuse to assume a role it
        # cannot see yet. The UI blocks harness creation until status=ready.
        if self._wait:
            self._sleep(self._wait)
        return team

    def apply_settings(self, team: TeamConfig) -> TeamConfig:
        """After an edit: narrow/widen the role's models, rewrite the Cedar
        action list, refresh the group description."""
        self._role_policy(team)
        ids = self._policies(team)
        try:
            self._cognito.update_group(UserPoolId=self._pool, GroupName=f"{GROUP_PREFIX}{team.name}",
                                       Description=team.label or team.name)
        except ClientError as exc:
            if _code(exc) not in _MISSING:
                raise ProvisionError(STEP_GROUP, f"{_code(exc)}: {exc}") from exc
        return team.model_copy(update={"policy_ids": ids})

    def deprovision(self, team: TeamConfig) -> None:
        """Reverse order; a piece already gone is not an error."""
        if self.policies_enabled:
            for pid in team.policy_ids.values():
                try:
                    self._delete_policy(pid)
                except ClientError as exc:
                    raise ProvisionError(STEP_POLICY, f"{_code(exc)}: {exc}") from exc
        rn = role_name(self._project, team.name)
        try:
            self._platform_role_arn(rn)
        except ProvisionError as exc:
            if "NoSuchEntity" not in exc.message:
                raise
        try:
            for pn in self._iam.list_role_policies(RoleName=rn).get("PolicyNames", []):
                self._iam.delete_role_policy(RoleName=rn, PolicyName=pn)
            self._iam.delete_role(RoleName=rn)
        except ClientError as exc:
            if _code(exc) not in _MISSING:
                raise ProvisionError(STEP_ROLE, f"{_code(exc)}: {exc}") from exc
        try:
            self._cognito.delete_group(UserPoolId=self._pool, GroupName=f"{GROUP_PREFIX}{team.name}")
        except ClientError as exc:
            if _code(exc) not in _MISSING:
                raise ProvisionError(STEP_GROUP, f"{_code(exc)}: {exc}") from exc

    # ── probes (drift) ─────────────────────────────────────────────────────
    def group_exists(self, team_name: str) -> bool:
        try:
            self._cognito.get_group(UserPoolId=self._pool, GroupName=f"{GROUP_PREFIX}{team_name}")
            return True
        except ClientError as exc:
            if _code(exc) in _MISSING:
                return False
            raise

    def role_exists(self, team: TeamConfig) -> bool:
        try:
            self._iam.get_role(RoleName=role_name(self._project, team.name))
            return True
        except ClientError as exc:
            if _code(exc) in _MISSING:
                return False
            raise

    def policies_exist(self, team: TeamConfig) -> Dict[str, bool]:
        if not self.policies_enabled:
            return {}
        return {kind: self._policy_exists(pid) for kind, pid in team.policy_ids.items()}
