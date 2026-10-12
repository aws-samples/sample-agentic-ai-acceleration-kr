import json
import logging
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from config import Config
from core.mcp_manager import MCPServerManager
from core.agent_manager import AgentManager
from tools.artifact_handler import ArtifactHandler
from utils.logger import LoggerSetup

# Initialize configuration and logging
config = Config.from_env()
LoggerSetup.setup_logging()
logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()

# Initialize managers
mcp_manager = MCPServerManager(config)
agent_manager = AgentManager(config, mcp_manager)


def _mcp_app_event(tool_name, tool_input, tool_use_id, message_id, tool_obj=None):
    """_meta.ui.resourceUri를 가진 툴 호출이면 앱 마운트 신호를 만든다.

    HTML은 더 이상 스트림에 실리지 않는다. 웹 호스트가 ui:// 리소스를 직접 가져가고,
    여기서는 "이 툴 호출에 앱이 붙는다"는 사실만 알린다.

    _meta는 MCPAgentTool.mcp_tool에서 읽는다 — tool_spec은 _meta를 버린다.
    """
    if tool_obj is None:
        return None

    try:
        source = getattr(tool_obj, "mcp_tool", None) or tool_obj
        meta = getattr(source, "meta", None) or getattr(source, "_meta", None)
        if not isinstance(meta, dict):
            return None

        ui_block = meta.get("ui")
        resource_uri = ui_block.get("resourceUri") if isinstance(ui_block, dict) else None
        if not isinstance(resource_uri, str) or not resource_uri.startswith("ui://"):
            return None

        return {
            "event": {
                "mcpApp": {
                    "toolCallId": tool_use_id,
                    "toolName": tool_name,
                    "resourceUri": resource_uri,
                    "messageId": message_id,
                }
            }
        }
    except Exception as exc:
        logger.warning("Could not extract MCP app signal for %s: %s", tool_name, exc)
        return None


def _maybe_artifact_event(tool_name, tool_input_str, tool_use_id, message_id):
    """Build an artifact injection event for artifact tools.

    Artifact tools only return a confirmation string, so the actual payload is
    reconstructed here from the tool name + accumulated JSON input and injected
    into the stream. Returns None for non-artifact tools or on parse failure.
    """
    if not ArtifactHandler.is_artifact_tool(tool_name):
        return None

    try:
        tool_input = json.loads(tool_input_str) if tool_input_str else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"Could not parse tool input for {tool_name}: {tool_input_str[:100]}")
        return None

    return ArtifactHandler.process_artifact_tool_call(
        tool_name, tool_input, tool_use_id, message_id
    )


@app.entrypoint
async def strands_agent_bedrock_streaming(payload, context):
    """Bedrock AgentCore streaming entrypoint.

    Streams the selected sample agent (AGENT_MODULE) and injects MCP App signals
    when the agent calls a tool with an attached ui:// resource.
    """
    # Keep-warm probe (EventBridge Scheduler pings this every few minutes). Return
    # before touching the model or the agent: a real invoke would run a model
    # call (and possibly tools) just to stay warm. The early
    # exit still keeps the expensive part hot — the microVM, the Python process, and
    # the imported modules — which is what a cold start pays for.
    if isinstance(payload, dict) and payload.get("ping"):
        yield {"pong": True}
        return

    user_message = payload.get("prompt")
    actor_id = payload.get("actor_id")

    if isinstance(user_message, list):
        # The server base64s raw bytes to survive JSON; Strands wants them back.
        import base64

        for block in user_message:
            for key in ("image", "document", "video"):
                source = (block.get(key) or {}).get("source") or {}
                data = source.get("bytes")
                if isinstance(data, str):
                    source["bytes"] = base64.b64decode(data)
        logger.info(f"Received {len(user_message)} content block(s)")
    else:
        logger.info(f"Received user message: {user_message}")

    # Fall back to the deployed configuration, not to a literal. The server's
    # _prepare_payload only sends prompt/system_prompt/actor_id, so these keys
    # are never present in practice and a literal default would win every time —
    # which made MODEL_ID and REGION_NAME dead env vars even though deploy.sh
    # injects both into every runtime.
    model_id = payload.get("model_id") or config.model_id
    region_name = payload.get("region_name") or config.region_name
    system_prompt = payload.get("system_prompt")
    # The server sets this on a thread's first turn, when the session holds no
    # prior events and recall would round-trip to Memory only to restore nothing.
    # Absent (older server, or a continuing turn) it stays False, so recall runs
    # exactly as before — the change only removes provably-empty reads.
    skip_recall = bool(payload.get("skip_recall", False))
    logger.info(f"Model ID: {model_id}, Region: {region_name}, Agent module: {config.agent_module}")
    logger.info(f"Runtime Session ID: {context.session_id}")

    # Stateful gateway tools (code interpreter, browser) are isolated per session.
    # This runtime authenticates to the gateway as one shared machine user, so
    # without this every conversation would share one sandbox and cookie jar.
    session_scope = getattr(context, "session_id", None)

    if not agent_manager.ensure_initialized(
        model_id=model_id,
        region_name=region_name,
        system_prompt=system_prompt,
        session_scope=session_scope,
        actor_id=actor_id,
        session_id=session_scope,
        skip_recall=skip_recall,
    ):
        yield {"error": "Failed to initialize agent. Please check the configuration and logs."}
        return

    runner = agent_manager.get_runner()
    if not runner:
        yield {"error": "Agent is not available"}
        return

    # Build a tool lookup dict by name for MCP App signal extraction.
    tool_lookup = {}
    try:
        agent_obj = getattr(runner, "get_agent", lambda: None)()
        if agent_obj is not None:
            registry = getattr(agent_obj, "tool_registry", None)
            if registry is not None:
                tool_lookup = getattr(registry, "registry", {}) or {}
    except Exception as exc:
        logger.warning(f"Could not build tool lookup for MCP apps: {exc}")

    # Track in-flight tool-use blocks so we can inject UI events when they complete.
    current_message_id = None
    tool_uses = {}  # tool_use_id -> {"name", "input"}
    current_tool_use_id = None

    async for event in runner.stream(user_message):
        yield event

        event_dict = event.get("event", {})

        if "messageStart" in event_dict:
            current_message_id = event_dict["messageStart"].get("id") or current_message_id
            tool_uses = {}
            current_tool_use_id = None

        elif "contentBlockStart" in event_dict:
            tool_use = event_dict["contentBlockStart"].get("start", {}).get("toolUse")
            if tool_use:
                tool_use_id = tool_use.get("toolUseId")
                tool_name = tool_use.get("name")
                if tool_use_id and tool_name:
                    current_tool_use_id = tool_use_id
                    tool_uses[tool_use_id] = {"name": tool_name, "input": ""}

                    # MCP app signal goes out as soon as the tool-use block opens: the
                    # host can then mount the view before the arguments finish
                    # streaming and feed them as `ui/notifications/tool-input-partial`
                    # (spec: "UI preloading"). Only the name and id are needed here,
                    # and both are in the start event.
                    app_event = _mcp_app_event(
                        tool_name, "", tool_use_id, current_message_id,
                        tool_obj=tool_lookup.get(tool_name),
                    )
                    if app_event:
                        yield app_event

        elif "contentBlockDelta" in event_dict:
            tool_use_delta = event_dict["contentBlockDelta"].get("delta", {}).get("toolUse", {})
            tool_input = tool_use_delta.get("input", "") if tool_use_delta else ""
            if tool_input:
                target_id = current_tool_use_id or (list(tool_uses)[-1] if tool_uses else None)
                if target_id and target_id in tool_uses:
                    tool_uses[target_id]["input"] += tool_input

        elif "contentBlockStop" in event_dict:
            # A tool-use block just finished; its input is fully accumulated.
            if current_tool_use_id and current_tool_use_id in tool_uses:
                tu = tool_uses[current_tool_use_id]

                # Artifact injection needs the full input, so it stays here. The MCP
                # app signal already went out at contentBlockStart.
                artifact_event = _maybe_artifact_event(
                    tu["name"], tu["input"], current_tool_use_id, current_message_id
                )
                if artifact_event:
                    yield artifact_event
            current_tool_use_id = None


if __name__ == "__main__":
    # Run the AgentCore Runtime App
    app.run()
