import logging
from auth import access_token
from typing import Optional, Tuple, Any
from config import Config

logger = logging.getLogger(__name__)


def filter_model_visible(tools: list) -> list:
    """visibility에 "model"이 없는 툴을 제거한다.

    규격(SEP-1865) MUST: 앱 전용 툴은 에이전트의 툴 목록에 넣어서는 안 된다. 넣으면
    UI 안에서만 눌러야 하는 동작을 모델이 임의로 호출한다.

    기본값은 ["model", "app"]이므로 _meta가 없는 기존 툴은 그대로 남는다. 형태가
    어긋난 경우에도 남긴다 — 조용히 사라지는 편이 더 나쁘다.
    """
    kept = []
    for tool in tools or []:
        try:
            source = getattr(tool, "mcp_tool", None) or tool
            meta = getattr(source, "meta", None) or getattr(source, "_meta", None)

            visibility = None
            if isinstance(meta, dict):
                ui_block = meta.get("ui")
                if isinstance(ui_block, dict):
                    visibility = ui_block.get("visibility")

            if isinstance(visibility, list):
                allowed = [v for v in visibility if v in ("model", "app")]
                if allowed and "model" not in allowed:
                    continue
        except Exception as exc:
            # 들여다볼 수 없는 툴은 남긴다. 하나가 예외를 던져서 목록 전체를 잃는 것이
            # 훨씬 나쁘다 — extract_tool_info에서 같은 유형의 사고가 있었다.
            logger.warning("Could not read visibility for a tool, keeping it: %s", exc)

        kept.append(tool)
    return kept


# Artifact tools are served locally by the default agent (`agents/default.py`)
# and intercepted by exact name in `main.py`, which turns the tool *input* into
# an `artifact` panel event. The shared gateway also advertises them so that
# harness-composed agents (which never run this runtime) can call them — but that
# means this runtime loads a second, gateway-prefixed copy (`<target>___create_
# artifact`) that the exact-name interception never catches. Left in, the model
# could pick a create_artifact that opens no panel. Drop the gateway copies; the
# local tools stay authoritative here.
_ARTIFACT_TOOL_NAMES = {"create_artifact", "update_artifact"}


def _tool_name(tool) -> str:
    """Best-effort read of a tool's model-facing name across tool shapes."""
    for getter in (
        lambda t: t.tool_name,
        lambda t: t.schema.name,
        lambda t: t.mcp_tool.name,
        lambda t: vars(t)["_name"],
    ):
        try:
            name = getter(tool)
            if isinstance(name, str) and name:
                return name
        except Exception:
            continue
    return ""


def drop_artifact_tools(tools: list) -> list:
    """Remove gateway-served artifact tools; the runtime uses its local copies.

    Matches the bare tool name after any gateway target prefix, so both
    `create_artifact` and `platform-tools___create_artifact` are dropped. A tool
    whose name cannot be read is kept — losing a whole list over one opaque
    proxy is worse than keeping one duplicate (same stance as filter_model_visible).
    """
    kept = []
    for tool in tools or []:
        name = _tool_name(tool)
        if name and name.split("___")[-1] in _ARTIFACT_TOOL_NAMES:
            logger.info("Dropping gateway artifact tool %r; runtime serves its own", name)
            continue
        kept.append(tool)
    return kept


class MCPServerManager:
    """Manages MCP server connection and health checks"""
    
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def is_server_running(self) -> bool:
        """Check if MCP server is running and accessible"""
        if not self.config.mcp_server_url:
            return False
        return True  # 실제 연결은 load_tools가 검증한다; SigV4 없는 사전 프로브는 403만 낸다
    
    def load_tools(self, session_scope: Optional[str] = None) -> Tuple[Optional[list], Optional[Any]]:
        """Load tools from MCP server with SigV4 authentication

        ``session_scope`` is accepted for compatibility but ignored by this gateway
        (no interceptor; all tools are stateless).
        """
        if not self.config.mcp_server_url:
            self.logger.error("MCP server URL is not configured, cannot load tools")
            return None, None
        try:
            tools, mcp_client = access_token.load_tools_from_mcp(
                self.config.mcp_server_url, session_scope=session_scope
            )
        except Exception as e:
            self.logger.error(f"Error loading tools from MCP server: {e}", exc_info=True)
            return None, None
        if not tools or not mcp_client:
            return None, None
        before = len(tools)
        tools = filter_model_visible(tools)
        if len(tools) != before:
            self.logger.info(f"Excluded {before - len(tools)} app-only tools")
        tools = drop_artifact_tools(tools)
        self._log_available_tools(tools)
        return tools, mcp_client
    
    def _log_available_tools(self, tools: list):
        """Log information about available tools"""
        if not tools:
            return
            
        tool_names = []
        for tool in tools:
            # Try different ways to get tool name
            if hasattr(tool, 'schema') and hasattr(tool.schema, 'name'):
                tool_names.append(tool.schema.name)
            elif hasattr(tool, 'tool_name'):
                tool_names.append(tool.tool_name)
            elif '_name' in vars(tool):
                tool_names.append(vars(tool)['_name'])
            else:
                tool_names.append(f"Tool-{id(tool)}")
        
        self.logger.info(f"Available tools: {', '.join(tool_names)}")
