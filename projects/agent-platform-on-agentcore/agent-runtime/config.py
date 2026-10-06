import os
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Config:
    """Configuration management for the agent runtime

    Minimal configuration required to run the agent:
    - model_id: Bedrock model ID (default: claude-sonnet-5-5)
    - region_name: AWS region for Bedrock (default: ap-northeast-1)
    - mcp_server_url: Optional MCP server URL (empty string means no MCP server)
    """
    model_id: str = "global.anthropic.claude-sonnet-5-5"
    region_name: str = "ap-northeast-1"
    mcp_server_url: str = ""
    max_retries: int = 2
    request_timeout: int = 10
    # Selects which sample agent to run (see agents/__init__.py AGENT_REGISTRY).
    agent_module: str = "default"
    # AgentCore Memory id. Empty means no memory: the hook is not attached and
    # the agent behaves as it did before memory existed.
    memory_id: str = ""
    # Long-term (session-summary) recall. Off by default so a runtime whose
    # memory has no summary strategy behaves exactly as before. Turned on per
    # runtime (bap_default) via the LONG_TERM_RECALL env.
    long_term_recall: bool = False
    # Bedrock Guardrail. Empty id means no guardrail: the model is built without
    # a guardrailConfig and behaves exactly as it did before. Both id and version
    # must be present for Bedrock to apply the guardrail (version defaults to the
    # always-available DRAFT so a single GUARDRAIL_ID env is enough to turn it on).
    guardrail_id: str = ""
    guardrail_version: str = "DRAFT"
    # Bedrock prompt caching for the static prefix (system prompt + tool schemas).
    # On by default because every turn after the first then reads that prefix from
    # cache at ~1/10 the input rate instead of re-billing it. Left as a flag so a
    # model or workload where caching is a net loss can turn it off without a
    # redeploy. Only takes effect on models that support cachePoint (see
    # `build_bedrock_model`).
    prompt_cache: bool = True
    # Max output tokens per model call. Strands' default (~4096) cuts long
    # answers off mid-sentence; 8192 fits a long report and is within every
    # Claude 4.5/5 model's output ceiling.
    max_tokens: int = 8192
    # Anthropic extended-thinking budget in tokens. 0 (default) means off: the
    # model is built exactly as before. Set per runtime via REASONING_BUDGET on
    # the runtimes that want it (bap_default), so the model
    # streams its reasoning before the answer — surfacing the reasoning box the
    # UI already renders and turning the silent pre-first-token wait into visible
    # "thinking". Costs extra tokens; does not lower total latency. Must stay
    # below max_tokens; build_bedrock_model clamps it if it is not.
    reasoning_budget: int = 0

    @classmethod
    def from_env(cls) -> 'Config':
        """Create config from environment variables

        Environment variables:
        - MODEL_ID: Bedrock model ID
        - REGION_NAME: AWS region for Bedrock
        - MCP_GATEWAY_URL: Optional MCP server URL (authenticated with SigV4)

        Falls back to defaults if environment variables are not set.
        """
        model_id = os.getenv("MODEL_ID", "global.anthropic.claude-sonnet-5-5")
        region_name = os.getenv("REGION_NAME", "ap-northeast-1")
        gateway_url = os.getenv("MCP_GATEWAY_URL", "")
        agent_module = os.getenv("AGENT_MODULE", "default")
        memory_id = os.getenv("MEMORY_ID", "")
        long_term_recall = os.getenv("LONG_TERM_RECALL", "false").strip().lower() not in (
            "false",
            "0",
            "no",
            "off",
            "",
        )
        guardrail_id = os.getenv("GUARDRAIL_ID", "")
        guardrail_version = os.getenv("GUARDRAIL_VERSION", "DRAFT")
        # Default on; any of the usual falsy spellings turns it off.
        prompt_cache = os.getenv("PROMPT_CACHE", "true").strip().lower() not in (
            "false",
            "0",
            "no",
            "off",
        )
        try:
            max_tokens = int(os.getenv("MAX_TOKENS", "8192"))
        except ValueError:
            max_tokens = 8192
        try:
            reasoning_budget = int(os.getenv("REASONING_BUDGET", "0"))
        except ValueError:
            reasoning_budget = 0

        logger.info(f"Loading config from environment variables")
        logger.info(f"Model ID: {model_id}")
        logger.info(f"Region: {region_name}")
        logger.info(f"Agent module: {agent_module}")

        if gateway_url:
            logger.info(f"MCP Gateway URL configured: {gateway_url}")
        else:
            logger.info("No MCP Gateway URL configured, will run with local tools only")

        if guardrail_id:
            logger.info(f"Guardrail configured: {guardrail_id} (version {guardrail_version})")
        else:
            logger.info("No guardrail configured, model runs without input/output filtering")

        return cls(
            model_id=model_id,
            region_name=region_name,
            mcp_server_url=gateway_url,
            agent_module=agent_module,
            memory_id=memory_id,
            long_term_recall=long_term_recall,
            guardrail_id=guardrail_id,
            guardrail_version=guardrail_version,
            prompt_cache=prompt_cache,
            max_tokens=max_tokens,
            reasoning_budget=reasoning_budget,
        )

    @classmethod
    def from_config_file(cls) -> 'Config':
        """Deprecated: Use from_env() instead.

        This method is kept for backwards compatibility.
        """
        logger.warning("from_config_file() is deprecated, use from_env() instead")
        return cls.from_env()
