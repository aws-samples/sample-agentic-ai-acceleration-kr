# Agent Runtime (`bap_default`)

The Strands agent this platform deploys to **Amazon Bedrock AgentCore Runtime**. One
module ships (`default`): a single agent that calls the platform's MCP gateway tools
(AgentCore Web Search, `fetch_url`, ...) and two local artifact tools, remembers the
conversation through AgentCore Memory, and streams Strands events back to the platform
server. The same runtime answers the platform's **basic chat** (model picked per turn).

Deploying it is optional — see [`DEPLOYMENT.md`](../DEPLOYMENT.md#runtime-에이전트와-기본-채팅)
for the end-to-end procedure. This file explains how the code is put together.

## How a turn flows

```
server/agents/agentcore_client.py ──InvokeAgentRuntime──▶ main.py
                                                           │ AgentManager (one runner per container,
                                                           │   rebuilt when model / session / actor change)
                                                           ├─ MCPServerManager ──SigV4──▶ bap-gateway (MCP tools)
                                                           ├─ MemoryHook ───────────────▶ AgentCore Memory
                                                           └─ Strands Agent ────────────▶ Bedrock (Converse, guardrail)
```

### Invocation payload

The platform server sends one JSON payload per turn:

| Key | Purpose |
|-----|---------|
| `prompt` | The user turn: a string, or a list of Converse content blocks (image/document bytes arrive base64-encoded and are decoded here) |
| `actor_id` | The end user's `sub`; scopes AgentCore Memory so a warm container never mixes two people's history |
| `system_prompt` | Optional per-turn override of the module's default prompt |
| `model_id` | Optional per-turn model. Set by the server for **basic chat** (operator-allowed model list); empty falls back to the runtime's `MODEL_ID` |
| `skip_recall` | `true` on a thread's first turn, so Memory is not read when it provably holds nothing |
| `ping` | Keep-warm probe (`scripts/keepwarm.sh`); answered with `{"pong": true}` before any model work |

The response is a stream of Strands-shaped `{"event": {...}}` dicts, plus two
platform-specific events `main.py` injects: `artifact` (a file the user can open in
the panel) and `mcpApp` (a gateway tool whose result renders as an MCP App).

### Gateway tools and session scope

`MCPServerManager` loads tools from `MCP_GATEWAY_URL` over streamable-http signed with
the runtime's execution role (`auth/sigv4.py`); the platform gateway uses `AWS_IAM`, so
no token or Cognito values are involved.

Stateful gateway tools (code interpreter, browser) keep a sandbox per *session*. Every
end user presents the same execution-role identity, so `main.py` passes the
`runtimeSessionId` down as the session scope and `AgentManager` rebuilds the runner
when it changes — a cached runner would otherwise run one conversation's code in
another's sandbox. How the gateway side binds that scope is described in
[`infra/builtin_tools_gateway`](../infra/builtin_tools_gateway/README.md#multi-user-isolation).

### Memory

With `MEMORY_ID` set, `memory/memory_hook.py` records each turn and restores the
conversation on the next one; with `LONG_TERM_RECALL=true` it also retrieves the
session summaries the Memory's `summaryMemoryStrategy` produces. Empty `MEMORY_ID`
means no hook at all. Each runtime should have its own Memory (`bap_conversations_<module>`)
— two runtimes sharing one would read each other's turns.

## Local setup

```bash
cd agent-runtime
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt       # or: uv sync  (pyproject.toml mirrors it)
cp .env.example .env
```

`.env.example` documents every variable. The ones that matter most:

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_ID` | `global.anthropic.claude-sonnet-5-5` | Bedrock model (a per-turn `model_id` in the payload overrides it) |
| `REGION_NAME` | `ap-northeast-1` | Region for Bedrock, Memory and the gateway |
| `AGENT_MODULE` | `default` | Which module under `agents/` to run |
| `AGENT_NAME` | `bap_<module>` | Runtime name. Leave unset — pinning it makes every module deploy onto one runtime |
| `MCP_GATEWAY_URL` | *(empty)* | Gateway MCP endpoint; empty = local tools only. `deploy.sh` fills it from `terraform output` |
| `EXECUTION_ROLE` | *(empty)* | Runtime execution role. Use the stack's `agent_runtime_role_arn`: the role `agentcore configure` creates on its own lacks `InvokeGateway` |
| `MEMORY_ID` | *(empty)* | AgentCore Memory id; empty = no memory |
| `LONG_TERM_RECALL` | `false` | Session-summary recall; only when the Memory has a summary strategy |
| `GUARDRAIL_ID` / `GUARDRAIL_VERSION` | *(empty)* / `DRAFT` | Bedrock Guardrail applied to every model call |
| `REASONING_BUDGET` | `0` | Extended-thinking budget in tokens; 0 = off. Claude 5 models get adaptive thinking instead of a budget |
| `MAX_TOKENS` | `8192` | Output ceiling per model call |
| `PROMPT_CACHE` | `true` | Cache the static prefix (system prompt + tool schemas) |
| `PLATFORM_API_URL`, `PLATFORM_ADMIN_USERNAME`, `PLATFORM_ADMIN_PASSWORD` | *(empty)* | Lets `deploy.sh` register the runtime in the Agent Registry; empty skips it |
| `AUTO_TF_OUTPUTS` / `INFRA_ENV_DIR` | `true` / `infra/envs/standalone` | Where `deploy.sh` reads Terraform outputs from |

Run locally with `uv run python main.py`; the AgentCore runtime server listens on
port 8080 and accepts `POST /invocations` with the payload above.

## Deploy

```bash
pip install bedrock-agentcore-starter-toolkit   # `agentcore` CLI, host only
./scripts/deploy.sh
```

`deploy.sh` runs `agentcore configure` → `agentcore launch` (CodeBuild, ARM64) → waits
for `READY` → tags the runtime for cost allocation → registers it in the Agent
Registry when `PLATFORM_*` is set. `agentcore configure` writes `Dockerfile`,
`.dockerignore` and `.bedrock_agentcore.yaml` into this directory on every run; all
three are git-ignored.

Other scripts:

| Script | Purpose |
|--------|---------|
| `scripts/keepwarm.sh up\|down` | EventBridge Scheduler that pings the runtime every `KEEPWARM_RATE` (default 5 min) so the first response is not a cold start |
| `scripts/enable_longterm_memory.py` | Creates (or reuses) a `bap_conversations_<module>` Memory with a summary strategy; the installer does the same for `default` |
| `scripts/logs.sh` | Tails the runtime's CloudWatch logs |

## Adding an agent module

Create `agents/<name>.py` exposing
`build(config, model, system_prompt, mcp_manager=None, session_scope=None)` that
returns a runner (`SingleAgentRunner` wraps a Strands `Agent`), register it in
`agents/__init__.py`, then deploy with `AGENT_MODULE=<name> ./scripts/deploy.sh`. It
becomes its own runtime, `bap_<name>`.

```python
# agents/my_agent.py
from strands import Agent
from agents.base import SingleAgentRunner

def build(config, model, system_prompt, mcp_manager=None, session_scope=None):
    tools = []
    if mcp_manager is not None:
        gateway_tools, _ = mcp_manager.load_tools(session_scope)
        tools.extend(gateway_tools or [])
    return SingleAgentRunner(Agent(model=model, tools=tools, system_prompt=system_prompt))
```

`AgentRuntimeName` allows `[a-zA-Z][a-zA-Z0-9_]{0,47}` — no hyphens, which is why the
runtime prefix is `bap_` while the infra prefix is `bap-`. Names cannot be changed in
place; renaming means a new runtime and re-pointing the registry.

## Tests

```bash
pytest tests/
```

Covers config resolution, SigV4 wiring, prompt caching, tool visibility, artifact
tool de-duplication and the memory hook. No AWS access is needed. To invoke a deployed
runtime once from the terminal, run `AGENT_RUNTIME_ARN=<arn> python scripts/test_main.py`.

## Layout

```
agent-runtime/
├── main.py                  # AgentCore Runtime entrypoint; injects artifact / mcpApp events
├── config.py                # Config.from_env()
├── agents/
│   ├── __init__.py          # AGENT_REGISTRY: module name -> build()
│   ├── base.py              # SingleAgentRunner, build_bedrock_model (guardrail, caching, thinking)
│   └── default.py           # The shipped agent
├── core/
│   ├── agent_manager.py     # One runner per container; rebuilds on model / session / actor change
│   └── mcp_manager.py       # Gateway tool loading and visibility filtering
├── auth/                    # SigV4 httpx client for the AWS_IAM gateway
├── memory/memory_hook.py    # AgentCore Memory record / restore / recall
├── prompts/prompt.py        # Default system prompt
├── tools/                   # create_artifact / update_artifact, the injection handler, example_tools.py (templates for your own tools)
├── utils/                   # logger.py, s3_util.py (optional S3 helper)
├── scripts/                 # deploy.sh, keepwarm.sh, enable_longterm_memory.py, logs.sh, test_main.py (invoke a deployed runtime once)
└── tests/
```
