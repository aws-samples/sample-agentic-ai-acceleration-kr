import logging
from typing import Optional, Any
from config import Config
from core.mcp_manager import MCPServerManager
from agents import get_builder
from agents.base import build_bedrock_model, AgentRunner


class AgentManager:
    """Selects and initializes a sample agent runner based on config.agent_module.

    The selected builder returns an AgentRunner exposing an async ``stream(prompt)``
    method, so single-agent and multi-agent samples share one streaming path.
    """

    def __init__(self, config: Config, mcp_manager: MCPServerManager):
        self.config = config
        self.mcp_manager = mcp_manager
        self.logger = logging.getLogger(__name__)
        self.runner: Optional[AgentRunner] = None
        # The scope the cached runner's MCP tools were bound to. A runtime container
        # is reused across sessions, so this is what tells us the cached runner
        # belongs to a different conversation.
        self.session_scope: Optional[str] = None
        self.actor_id: Optional[str] = None
        self.model_id: Optional[str] = None

    def initialize(
        self,
        model_id: Optional[str] = None,
        region_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        session_scope: Optional[str] = None,
        actor_id: Optional[str] = None,
        session_id: Optional[str] = None,
        skip_recall: bool = False,
    ) -> bool:
        """Build the agent runner selected by config.agent_module."""
        try:
            agent_module = getattr(self.config, "agent_module", "default")
            self.logger.info(f"Initializing agent module: {agent_module}")

            builder = get_builder(agent_module)
            model = build_bedrock_model(self.config, model_id, region_name)

            self.runner = builder(
                self.config,
                model,
                system_prompt,
                mcp_manager=self.mcp_manager,
                session_scope=session_scope,
                hooks=self._memory_hooks(actor_id, session_id, skip_recall),
            )
            self.session_scope = session_scope
            self.actor_id = actor_id
            self.model_id = model_id
            self.logger.info(f"Agent module '{agent_module}' initialized successfully")
            return True

        except Exception as e:
            self.logger.error(f"Error initializing agent: {str(e)}", exc_info=True)
            return False

    def _memory_hooks(self, actor_id, session_id, skip_recall=False):
        """A MemoryHook when a memory is configured, else nothing.

        Without a memory id the agent must behave exactly as it did before
        memory existed, so local runs stay unaffected.
        """
        memory_id = getattr(self.config, "memory_id", "")
        if not (memory_id and actor_id and session_id):
            return []

        try:
            # Imported inside the guard, not above it: memory is supplementary, so
            # a missing dependency has to degrade recall like any other memory
            # failure. Outside the try an ImportError would propagate and fail
            # agent initialization entirely — an agent with no recall beats no
            # agent at all.
            from bedrock_agentcore.memory import MemoryClient

            from memory.memory_hook import MemoryHook

            client = MemoryClient(region_name=getattr(self.config, "region_name", None))
            return [
                MemoryHook(
                    memory_client=client,
                    memory_id=memory_id,
                    actor_id=actor_id,
                    session_id=session_id,
                    long_term_recall=getattr(self.config, "long_term_recall", False),
                    skip_recall=skip_recall,
                )
            ]
        except Exception as exc:
            self.logger.warning(f"Memory unavailable, continuing without it: {exc}")
            return []

    def is_initialized(self) -> bool:
        return self.runner is not None

    def get_runner(self) -> Optional[AgentRunner]:
        return self.runner

    def ensure_initialized(
        self,
        model_id: Optional[str] = None,
        region_name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        session_scope: Optional[str] = None,
        actor_id: Optional[str] = None,
        session_id: Optional[str] = None,
        skip_recall: bool = False,
    ) -> bool:
        """Ensure the runner is initialized; reinitialize if a system_prompt is given."""
        # A per-request system_prompt override forces a rebuild.
        if self.is_initialized() and system_prompt:
            self.logger.info("System prompt provided, reinitializing agent...")
            self.runner = None

        # MCP tools are bound to the connection they were listed from, and that
        # connection carries the session scope. Reusing a runner built for another
        # conversation would run this one's code in that conversation's sandbox.
        if self.is_initialized() and session_scope != self.session_scope:
            self.logger.info(
                f"Session scope changed ({self.session_scope} -> {session_scope}), "
                "reinitializing agent..."
            )
            self.runner = None

        # A cached runner carries the previous caller's memory hook, which would
        # write this conversation into that user's memory.
        if self.is_initialized() and actor_id != self.actor_id:
            self.logger.info("Actor changed, reinitializing agent...")
            self.runner = None

        # The runner is built around one Bedrock model. A warm container serving
        # a basic-chat turn that picked another model must not answer with the
        # previous turn's model.
        if self.is_initialized() and model_id != self.model_id:
            self.logger.info(
                f"Model changed ({self.model_id} -> {model_id}), reinitializing agent..."
            )
            self.runner = None

        if self.is_initialized():
            return True

        self.logger.info("Agent not initialized, attempting initialization...")
        return self.initialize(
            model_id=model_id,
            region_name=region_name,
            system_prompt=system_prompt,
            session_scope=session_scope,
            actor_id=actor_id,
            session_id=session_id,
            skip_recall=skip_recall,
        )
