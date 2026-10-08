# Agent clients

`services/streaming_service.py` runs a chat turn through one of two clients and
relays what they yield as Server-Sent Events. Both implement `AgentClient`
(`base.py`): `async execute_stream(thread_id, values, config, actor_id)` yielding
Strands-shaped `{"event": {...}}` dicts, so the streaming service, the stored
transcript and the web consume one event shape regardless of which AgentCore API
actually ran the agent.

| File | Role |
|------|------|
| `base.py` | `AgentClient` interface |
| `agentcore_client.py` | **Code-deployed agents** (`agent-runtime/`). Calls `InvokeAgentRuntime` on the ARN the registry record (or `AGENT_RUNTIME_ARN`) names, derives the `runtimeSessionId` from the thread id so AgentCore Memory stays per thread, and normalises the runtime's SSE frames. The platform runtime already speaks Strands events; the other branches accept the simpler `text_delta`/`final` and typed `text`/`tool_call`/`done` shapes so a third-party runtime can be registered without a wrapper. |
| `harness_client.py` | **No-code Harness agents**. Calls `InvokeHarness` with per-turn `model`/`systemPrompt`/`tools` overrides and feeds the response through `HarnessEventAdapter`. |
| `harness_event_adapter.py` | Translates InvokeHarness frames (content blocks, tool use, tool-result *deltas*, `status`) into Strands events. Tool results arrive as deltas and the status only on the block start, so the adapter accumulates rather than assigns. |
| `harness_command_client.py` | Runs a shell command inside a harness session container (`InvokeAgentRuntimeCommand`). Used by `harness_output_service.py` to collect files the harness wrote into its sandbox. |
| `message_utils.py` | `build_strands_conversation`: turns the stored thread transcript into the conversation payload the harness path sends. |
| `agent_config.py` | Env-backed defaults: region, `AGENT_RUNTIME_ARN`, runtime qualifier. |
| `formatters/event_formatter.py` | Builders for the Strands event shapes (`messageStart`, `contentBlockDelta`, `toolResult`, `metadata`, `agentStatus`, `chart`, `verification`, `end`, ...) used by the two clients and the streaming service. |

## Event shape

Every yielded dict is `{"event": {<one key>: {...}}}`. The keys the web renders:

- `messageStart`, `contentBlockStart`, `contentBlockDelta`, `contentBlockStop`,
  `messageStop` — Strands/Converse stream, including `reasoningContent` deltas and
  `toolUse` blocks.
- `toolResult` — the result of a tool call (`status` present only on the harness path).
- `metadata` — token usage and guardrail trace, which `usage_service` turns into the
  Insights ledger.
- `agentStatus` — heartbeat / progress line while a tool runs.
- `artifact`, `mcpApp` — platform extensions: a file for the side panel, or a gateway
  tool whose result renders as an MCP App (`agent-runtime/main.py` emits these;
  `streaming_service` also synthesises them for harness tool results).
- `chart`, `verification` — optional extras a runtime may attach to a turn.
- `error`, `end`.

The streaming service persists the assistant message on `messageStop`, on a stream that
ends without one, and on a severed connection, so a thread cut mid-answer still comes
back from DynamoDB with the partial turn.
