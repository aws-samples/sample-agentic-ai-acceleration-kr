# gateway_policies

Cedar policies on the AgentCore policy engine that guards the Lambda tools
gateway (`modules/policy_engine` creates the engine, `modules/mcp_gateway`
attaches it).

Creates:
- `<project>_platform_all_tools` — platform roles (agent runtime, default harness, server task role, `extra_platform_role_names`) may call every tool.
- `<project>_platform_team_only_forbid` — platform roles may not call `team_only_actions` (the workshop mock tools `approve_expense`, `lookup_salary` when `demo_tools`). Forbid wins over the permit above, so these tools are reachable only through a team policy.
- `<project>_demo_expense_limit` — `approve_expense` for `demo_expense_role_names`, only when `context.input.amount` is below `expense_limit_usd` (workshop demo; matches roles by name).

Team policies are not terraform's. The Organization page (`/org`) creates each
team's execution role and its Cedar permits at runtime
(`server/services/team_provisioner.py`): `<project>_team_<team>_tools` for the
team's gateway tools and `<project>_team_<team>_shared` for `TEAM_SHARED_ACTIONS`
(the web-search connector's `<project>-web-search___WebSearch` by default when
`web_search_backend = "agentcore"`). Both use the same principal condition as
the policies here.

Principal type: every statement scopes `principal is AgentCore::IamEntity`
(the gateway is AWS_IAM). An untyped `principal` also matches
`AgentCore::UnauthenticatedUser`, which has no `id`, and the validator rejects
the policy; Cloud Control reports only `InternalFailure`, so read the reason
from `ListPolicies` → `statusReasons`.

Principal format: `principal.id` is the STS assumed-role ARN, matched as
`== "arn:aws:sts::<acct>:assumed-role/<role>"` or
`like "arn:aws:sts::<acct>:assumed-role/<role>/*"` (session suffix). There is no
bare prefix wildcard, so one role name being a prefix of another cannot leak.
Actions are `<target_name>___<tool>`; every policy names the gateway ARN as resource.

Names must match `[A-Za-z][A-Za-z0-9_]*` (max 48), so hyphens in the project
name are replaced with `_` in policy names only.

Built-in tools gateway (`infra/builtin_tools_gateway/deploy.py`): attach the
engine with `--policy-mode LOG_ONLY` only. That gateway has no baseline policies
here, so ENFORCE would deny every call; its team isolation is the harness
`allowedTools` `@builtin` entry.
