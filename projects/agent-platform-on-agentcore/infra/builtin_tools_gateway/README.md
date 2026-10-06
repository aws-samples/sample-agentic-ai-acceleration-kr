# builtin_tools_gateway

Deploys one AgentCore MCP Gateway carrying every built-in tool AgentCore provides —
**web search**, **code interpreter** and **browser** — so an agent or harness gets
all of them from a single endpoint.

```
agent / harness ──MCP──▶ Gateway ─┬─ connector target ──▶ Web Search (managed)
                                  │
                                  ├─ REQUEST interceptor ─▶ Lambda (stamps caller identity)
                                  │
                                  └─ lambda target ──────▶ Lambda ─┬─▶ Code Interpreter
                                                                   └─▶ Browser
```

One code interpreter and one browser resource serve everybody — the *session* is the
isolation unit, not the resource — and the interceptor is what makes each caller land
in their own session. See [Multi-user isolation](#multi-user-isolation).

## Why this is a script and not Terraform

Only web search is a real gateway target. The other two are not, and that shapes
everything here:

| Built-in tool | How it attaches |
|---|---|
| Web Search | Native gateway target — `mcp.connector`, `connectorId: "web-search"` |
| Code Interpreter | **No target type.** Data-plane API (`aws.codeinterpreter.v1`), bridged via Lambda |
| Browser | **No target type.** Data-plane API (`aws.browser.v1`), bridged via Lambda |

`CreateGatewayTarget` accepts `openApiSchema`, `smithyModel`, `lambda`, `mcpServer`,
`apiGateway` and `connector`; the only built-in connectors are `web-search` and
`bedrock-knowledge-bases`. Code interpreter and browser are separate primitives with
their own `Start*Session` / `Invoke*` APIs, so putting them "on the gateway" means
bridging them through a Lambda MCP target — which is what `lambda/handler.py` does.

On top of that, the `awscc` provider still has no
`awscc_bedrockagentcore_gateway_target` resource, which is why the existing
[`../modules/mcp_gateway`](../modules/mcp_gateway) already drives its target through
boto3. Rather than split this stack across Terraform and a provisioner script, it all
lives in one idempotent script.

## Usage

```bash
./deploy.py up                 # create or update everything
./deploy.py status             # gateway, targets, and the tool list
./deploy.py test               # call the tools over MCP
./deploy.py down               # delete everything it created
```

Global flags go **before** the subcommand: `./deploy.py --region us-west-2 up`.
Requires `python3` with `boto3`, and credentials that can create IAM roles, Lambda
functions, an S3 bucket and a gateway.

Useful options:

```bash
./deploy.py up --no-web-search                 # skip the web search connector
./deploy.py up --cognito-user-pool-id POOL \
              --cognito-client-id CLIENT       # CUSTOM_JWT instead of AWS_IAM
./deploy.py up --shared-sessions               # one sandbox for everyone (single-user only)
./deploy.py test --bearer-token "$TOKEN"       # test a CUSTOM_JWT gateway
./deploy.py test --scope thread-2              # test a second sandbox for the same caller
./deploy.py down --delete-bucket               # also remove screenshots
```

## Inbound auth

Defaults to **`AWS_IAM`** (SigV4), which is what both a harness and this repo's own
agent runtime use. The runtime (`agent-runtime/auth/sigv4.py`) signs gateway requests
with SigV4 using its execution-role credentials — no Cognito setup, no bearer token.
Give the runtime only the endpoint:

```
MCP_GATEWAY_URL=<mcp_endpoint without /mcp>   # the runtime appends /mcp
```

Pass `--cognito-user-pool-id` and `--cognito-client-id` for **`CUSTOM_JWT`** only if a
custom (non-runtime) client needs bearer tokens; then give it `COGNITO_CLIENT_ID` /
`COGNITO_USERNAME` / `COGNITO_PASSWORD`.

A gateway's authorizer type **cannot be changed after creation**. Switching means
`down` first, or deploying under a different `--project`.

## Attaching to a harness

```bash
agentcore add tool --harness my-agent --type agentcore_gateway \
  --name builtin-tools --gateway-arn <gateway_arn from ./deploy.py up>
```

The caller needs `bedrock-agentcore:InvokeGateway` on the gateway ARN. Note that a
harness can also take browser and code interpreter *directly*
(`--type agentcore_browser`, `--type agentcore_code_interpreter`) without a gateway —
route them through this gateway when you want one governed tool surface, Cedar
policies, or non-harness clients to reach them too.

## Tools

`WebSearch` comes from the connector. The rest are the Lambda bridge:

| Tool | Notes |
|---|---|
| `execute_code` | Python / JavaScript / TypeScript, state persists per session |
| `execute_command` | Shell in the same sandbox, e.g. `pip install pandas` |
| `write_files` / `read_files` / `list_files` / `remove_files` | Sandbox filesystem |
| `stop_code_session` | Discard your own sandbox early |
| `browser_navigate` | Opens a URL |
| `browser_screenshot` | Returns a presigned PNG URL |
| `browser_click` / `browser_type` / `browser_press` / `browser_shortcut` / `browser_scroll` | Interaction |
| `browser_stop` | Close your own browser session early |

Tools reach the agent as `<target>___<tool>`, e.g. `builtin-tools___execute_code`,
`web-search___WebSearch`.

### Sessions

Both primitives are stateful; Lambda is not. The bridge therefore rediscovers the
session id by name on each call (`list_*_sessions` filtered to `READY`), so
`value = 42` in one call is still there for `print(value)` in the next, even across
cold starts. Sessions idle out after 30 minutes (`SESSION_TIMEOUT_SECONDS`).

No tool takes a `session` argument. The label is derived from the caller — see below.

## Multi-user isolation

A shared code interpreter is a shared filesystem, and a shared browser is a shared
cookie jar, so the session label decides who can read whose data. **The label is not
something a caller can choose.**

That constraint exists because of what a Lambda MCP target can see. The gateway hands
a target only `client_context.custom` — gateway id, target id, tool name, request id —
with no caller identity, under both `AWS_IAM` and `CUSTOM_JWT`. Custom HTTP headers and
MCP `params._meta` are dropped too. The only channel reaching the target is
`params.arguments`, which the *model* writes. A model-supplied `session: "someone-else"`
would therefore be a free pass into another user's sandbox.

A **gateway REQUEST interceptor** is the one place that sees both the gateway-verified
identity and the request body, so `interceptor/handler.py` derives the label there and
overwrites whatever the model sent:

| Inbound auth | Identity used |
|---|---|
| `AWS_IAM` | `context.identity.awsPrincipalArn` |
| `CUSTOM_JWT` | `sub` from the bearer token (the gateway leaves `context.identity` empty) |

The label is `<readable tail>-<sha256 prefix>`, e.g. `alice-aaef11ab`, written to
`__session`. `session` is stripped from the arguments, and the bridge **only** trusts
`__session` (`REQUIRE_TRUSTED_SESSION`) — so with no interceptor attached every tool
call fails loudly instead of silently sharing one sandbox. A caller with no resolvable
identity is denied (`REQUIRE_IDENTITY`). Only bridge tools are stamped; the web search
connector is stateless and passes through untouched.

### One identity, many conversations

Send `X-Agent-Session-Scope: <id>` to subdivide your *own* sandbox — one per thread
rather than one per account. It is appended **under** the verified identity
(`alice-aaef11ab-thread-2-66c6514d`), so it can never reach another caller.
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` is honoured the same way, which is what
an AgentCore Runtime already sends.

Both halves of the label are `<readable>-<sha256 prefix>`, and the digest is taken over
the *full* value. That matters more than it looks: a runtime session id is derived from
a thread id and padded to at least 33 characters, so on truncation alone two threads
sharing a prefix would land in the same sandbox.

This repo's **`agent-runtime` does not send the scope header today.** `deploy.sh` wires
it to the Lambda tool gateway (`infra/modules/mcp_gateway`, `MCP_GATEWAY_URL`), whose tools
are stateless and which has no interceptor, so `auth/access_token.py` accepts a
`session_scope` argument for call-site compatibility and ignores it. What the runtime
*does* keep is the plumbing a scoped gateway needs: `context.session_id` (the
`runtimeSessionId` the server derives from `thread_id`) flows through
`AgentManager.ensure_initialized` → `MCPServerManager.load_tools` →
`load_tools_from_mcp`, and `AgentManager` rebuilds the runner whenever that scope changes,
because MCP tools are bound to the connection they were listed from — a reused container
must not run one conversation's code in another's sandbox. To point the runtime at this
gateway, set `MCP_GATEWAY_URL` to its endpoint and add the header in
`load_tools_from_mcp` (see `mcp_client.py` for the shape). An AgentCore Runtime caller
that already sends `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` is scoped with no
further change.

### Escaping the shared sandbox

`./deploy.py up --shared-sessions` reverts to one sandbox for everyone (no interceptor,
`REQUIRE_TRUSTED_SESSION=false`). Only reasonable for a single-user deployment.

### Concurrency

Quotas are 1000 concurrent sessions each for code interpreter and browser (adjustable),
so one resource per account is not a bottleneck. AgentCore does **not** enforce unique
session names, so simultaneous first calls from one identity can create duplicates; the
bridge settles that after the fact by keeping the oldest session and stopping the rest,
which converges without any coordination between Lambda invocations.

### Screenshots

MCP tool results are text, so screenshots go to S3 and the tool returns a presigned
URL (1 hour, bucket private, objects expire after 7 days).

## Caveats

- **Web search is offered in us-east-1, eu-west-1 and ap-northeast-1.** In any other
  region the script logs that and deploys the other two tools; pass
  `--no-web-search` to skip it deliberately.
- **Browser control is OS-level, not DOM-level.** `InvokeBrowser` moves a mouse and
  types keys — there is no "click the login button" or "read the page text". Clicks at
  empty coordinates still report `SUCCESS`, so an agent must screenshot to see what
  actually happened. For DOM automation, drive the session's CDP `automationStream`
  endpoint with Playwright instead of going through this gateway.
- `browser_type` is **ASCII-only**; non-ASCII characters are silently skipped by the
  service.
- Newly created IAM roles are eventually consistent, and AgentCore reports that as
  "not authorized to perform AssumeRole" / "lacks permission". The script retries
  those for up to two minutes.
- **Attaching or detaching the interceptor puts the gateway in `UPDATING`**, and every
  target write in that window is rejected with "Cannot perform operation ... when
  gateway is in UPDATING status". `up` waits for `READY` before touching targets.
- Sessions are owned by the Lambda's execution role, so `list-code-interpreter-sessions`
  run under your own credentials returns nothing. Read the Lambda logs to see which
  session a call resolved to.
- **`down` then `up` recreates the execution role, which orphans the Lambda's KMS
  grant.** Lambda encrypts environment variables under the `aws/lambda` key and binds
  the decrypt grant to the role's unique id, not its name — so a same-named replacement
  role cannot decrypt, every tool call fails with `KMSAccessDeniedException`, and the
  agent only sees "An internal error occurred". Updating the function does not refresh
  the grant, so `up` detects this and replaces the function. If you ever hit it outside
  this script, recreate the function rather than re-saving its configuration.
- The gateway, its targets and the Lambda are not tracked in Terraform state, so
  out-of-band changes are not detected as drift. Re-running `up` converges them.
- `down` leaves the screenshot bucket unless `--delete-bucket` is passed.
