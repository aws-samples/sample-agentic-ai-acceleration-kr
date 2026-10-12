"""Default agent: preserves the original single-agent scaffold behavior.

Uses MCP tools (if a gateway is configured) plus any local tools. This is the
backwards-compatible path for deployments that don't set AGENT_MODULE.
"""
import logging
from typing import Optional

from strands import Agent
from strands.models import BedrockModel

from agents.base import SingleAgentRunner
from prompts.prompt import ORCHESTRATOR_PROMPT
from tools.artifact_tools import create_artifact, update_artifact

logger = logging.getLogger(__name__)


def build(config, model: BedrockModel, system_prompt: Optional[str], mcp_manager=None, session_scope=None, hooks=None) -> SingleAgentRunner:
    local_tools = [create_artifact, update_artifact]

    mcp_tools = []
    if mcp_manager is not None:
        try:
            loaded, _mcp_client = mcp_manager.load_tools(session_scope=session_scope)
            if loaded:
                mcp_tools = loaded
                logger.info(f"Loaded {len(mcp_tools)} MCP tools")
        except Exception as e:
            logger.warning(f"MCP tools unavailable, continuing with local tools: {e}")

    agent = Agent(
        model=model,
        tools=mcp_tools + local_tools,
        system_prompt=system_prompt or ORCHESTRATOR_PROMPT,
        hooks=hooks or [],
    )
    return SingleAgentRunner(agent)
