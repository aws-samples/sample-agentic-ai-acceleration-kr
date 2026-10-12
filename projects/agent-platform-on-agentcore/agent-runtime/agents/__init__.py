"""Sample agent registry.

Each entry maps an AGENT_MODULE value to a builder function with signature:
    build(config, model, system_prompt, mcp_manager=None, session_scope=None) -> AgentRunner

``session_scope`` is forwarded to mcp_manager.load_tools so stateful gateway tools
get one sandbox per conversation instead of one per deployment.

AgentManager picks the builder by config.agent_module.
"""
from agents import default

AGENT_REGISTRY = {
    "default": default.build,
}


def get_builder(agent_module: str):
    """Return the builder for the given module name, falling back to default."""
    return AGENT_REGISTRY.get(agent_module, AGENT_REGISTRY["default"])
