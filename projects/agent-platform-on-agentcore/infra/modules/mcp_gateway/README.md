# mcp_gateway

Creates an AgentCore MCP Gateway with a Lambda target carrying the platform's own
tools, plus web search chosen by `web_search_backend`:

- `agentcore` (default) — the native AgentCore **Web Search Tool** connector as its
  own target (`<project>-web-search___WebSearch`). No API key.
- `none` — no web search on the gateway.

```
agent runtime ──SigV4(AWS_IAM)──▶ Gateway ──┬─ assume role ─▶ Lambda (current_time, calculate, fetch_url, artifacts)
                                            └─ assume role ─▶ Web Search connector (WebSearch)   # web_search_backend = agentcore
```

## Why the targets are a script

`awscc_bedrockagentcore_gateway` exists, so the gateway itself is a real Terraform
resource. Its **targets are not**: `awscc` (through provider 1.94.0) has no
`awscc_bedrockagentcore_gateway_target`, even though
`AWS::BedrockAgentCore::GatewayTarget` is a live CloudFormation type. Both targets
are therefore reconciled by `scripts/gateway_target.py` via boto3, driven by
`null_resource` provisioners — the same pattern as `../agent_registry`.

## Authorization

`AWS_IAM` (SigV4), **not** `CUSTOM_JWT`. The agent runtime and harness call the
gateway with **execution-role SigV4**: the runtime's MCP client
(`agent-runtime/auth/sigv4.py`) detects the gateway domain and signs each request
with the execution role's temporary credentials. The old Cognito machine-user /
bearer-token path is gone — there is no dedicated Cognito user for the gateway.

`deploy.sh` grants the runtime's execution role `bedrock-agentcore:InvokeGateway`.
Pass only the endpoint to the runtime (no `COGNITO_*` / `BEARER_TOKEN`):

```
MCP_GATEWAY_URL=<gateway_url without /mcp>   # the runtime appends /mcp
```

> `authorizer_type` is **create-only** — flipping it replaces the gateway resource
> (a cutover, not an in-place update).

## Tools

| Tool | Target | When |
|------|--------|------|
| `current_time` | Lambda (`platform-tools`) | always |
| `calculate` | Lambda (`platform-tools`) | always |
| `fetch_url` | Lambda (`platform-tools`) | always |
| `create_artifact` / `update_artifact` | Lambda (`platform-tools`) | always |
| `WebSearch` | connector (`web-search`) | `web_search_backend = agentcore` |

Tools are exposed to the agent as `<target-name>___<tool-name>`
(e.g. `bap-platform-tools___fetch_url`, `bap-web-search___WebSearch`).

### Adding a Lambda tool

1. Implement it in `lambda/handler.py` and register it in `TOOLS`.
2. Declare its schema in `tools.json`.
3. `terraform apply` — the schema hash change re-runs the target upsert.

The Lambda ships as a bare `handler.py` with **no third-party packages**, so tools
must stick to the standard library plus `boto3`.

### Web search

- **agentcore** (default): the native AgentCore **Web Search Tool**
  (`connectorId: web-search`) as its own connector target — no API key, no
  secret. The gateway role gets `bedrock-agentcore:InvokeGateway` +
  `InvokeWebSearch`. Available in US East (N. Virginia), Europe (Ireland) and
  Asia Pacific (Tokyo); in any other region set `web_search_backend = "none"`, or
  the target creation fails. The target is pinned to
  `web_search_connector_version` (default `1.2.0`, which adds request-level
  domain and published-date filters); empty uses the connector's default version.
  Pinning a version needs botocore >= 1.43.78 on the apply host — older SDKs do
  not model `source.version` and fail the upsert.
- **none**: no web search tool on the gateway. Agents still read a given URL with
  `fetch_url`.

Switching is a plain `terraform apply`: the connector target is gated by the value.

## Caveats

- `protocol_type` is omitted: the awscc provider validates it as a JSON string and
  rejects a plain `"MCP"`. MCP is the only supported value, so the default applies.
- The gateway role's `lambda:InvokeFunction` grant is eventually consistent;
  `CreateGatewayTarget` can reject it as missing, so the script retries.
- The target is not a tracked Terraform resource, so out-of-band changes to it are
  not detected as drift. `terraform destroy` does remove it.
- Requires `python3` with `boto3`/`botocore` (>= 1.43.78 to pin a connector
  version) wherever `terraform apply` runs.

## Workshop mock tools (`demo_tools`)

`approve_expense` and `lookup_salary` are canned mocks for the team/policy
workshop. They stay in `tools.json` but the upsert drops them
(`--drop-tool`) unless `demo_tools = true`. The flag is not a `null_resource`
trigger (adding one would replace the live target), so flipping it on an
existing stack needs `terraform apply -replace=module.mcp_gateway.null_resource.target`.

## Target replacement is destroy-first

`null_resource.target` is replaced whenever a trigger changes (schema sha,
script sha, Lambda ARN, …). Terraform runs the old resource's destroy
provisioner (`gateway_target.py delete`) **before** the new upsert, and
`create_before_destroy` is not a fix: the upsert reconciles by name, so the
later destroy would delete the target it just upserted. If a later step in the
same apply fails, the Lambda tools target is left deleted and every agent loses
`current_time`, `calculate`, `fetch_url`, `create_artifact`, …

Seen on bap 2026-10-10, twice: at ~02:10 UTC a registry-module replace failed
(SystemExit) after the target had been deleted, and at ~04:12:45 UTC the
policy-engine attach failed on IAM propagation after the same delete; the
second time the target stayed gone until the next apply recreated it at
04:21:08 (≈8 min 20 s).

Recovery: fix the failing step and re-run `terraform apply` — the null_resource
is still pending creation, so the apply recreates the target. To restore it
without fixing anything else, `terraform apply -target=module.mcp_gateway.null_resource.target`.
Check with `aws bedrock-agentcore-control list-gateway-targets --gateway-identifier <id>`.
