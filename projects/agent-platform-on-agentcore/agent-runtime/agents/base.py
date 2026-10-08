"""Common helpers for sample agents.

Every sample agent exposes a ``build(config, model, system_prompt, mcp_manager=None,
session_scope=None)`` function that
returns a *runner*: an object with ``async def stream(prompt) -> AsyncIterator[dict]``
yielding Strands-shaped ``{"event": {...}}`` dicts. ``main.py`` consumes runners
uniformly, so every sample shares one streaming path.
"""
import re
import logging
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Protocol, Union

from strands import Agent
from strands.models import BedrockModel

logger = logging.getLogger(__name__)

# Claude 5 (Sonnet 5, Opus 5.x, Haiku 5) rejects the budgeted thinking block —
# ConverseStream answers '"thinking.type.enabled" is not supported for this model.
# Use "thinking.type.adaptive"' — so those models get adaptive thinking instead.
# Matched on the model id because the switch is per model, not per deployment:
# basic chat picks the model per turn on a runtime whose REASONING_BUDGET is set.
_ADAPTIVE_THINKING_MODELS = re.compile(r"claude-(?:opus|sonnet|haiku)-5(?:[-.]|$)")


def thinking_fields(model_id: str, budget: int) -> dict:
    """The additionalModelRequestFields that turn reasoning on for `model_id`."""
    if _ADAPTIVE_THINKING_MODELS.search(model_id or ""):
        return {"thinking": {"type": "adaptive"}}
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


# Anthropic's floor for an extended-thinking budget; a smaller value is rejected.
# Also the answer headroom kept when a configured budget crowds max_tokens.
MIN_REASONING_BUDGET = 1024


class AgentRunner(Protocol):
    """Uniform streaming interface consumed by main.py."""

    async def stream(self, prompt: Union[str, List[Dict[str, Any]]]) -> AsyncIterator[Dict[str, Any]]:
        ...


class SingleAgentRunner:
    """Wraps a Strands ``Agent`` and forwards its native events unchanged."""

    def __init__(self, agent: Agent):
        self.agent = agent

    def get_agent(self) -> Agent:
        """Return the underlying Strands Agent for tool introspection."""
        return self.agent

    async def stream(self, prompt: Union[str, List[Dict[str, Any]]]) -> AsyncIterator[Dict[str, Any]]:
        async for event in self.agent.stream_async(prompt):
            if "event" in event:
                yield event


def build_bedrock_model(config, model_id: Optional[str], region_name: Optional[str]) -> BedrockModel:
    """Construct a BedrockModel from explicit args with config fallbacks."""
    final_model_id = model_id or config.model_id
    final_region = region_name or getattr(config, "region_name", None)

    kwargs: Dict[str, Any] = {"model_id": final_model_id}
    if final_region:
        kwargs["region_name"] = final_region

    # Raise the output ceiling above Strands' ~4096 default, which cuts long
    # answers off mid-sentence. Per runtime via MAX_TOKENS; 8192 is within every
    # supported model's ceiling.
    max_tokens = getattr(config, "max_tokens", 0)
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    # Attach a Bedrock Guardrail only when one is configured, so unconfigured
    # runs build the model exactly as before. Strands applies guardrailConfig on
    # every Converse call (and detects blocks / redacts mid-stream on its own)
    # whenever both id and version are set; redact behaviour is left at the
    # Strands defaults (input redacted, output blocked via the guardrail's own
    # blockedMessaging). guardrail_trace stays enabled so blocks are logged.
    guardrail_id = getattr(config, "guardrail_id", "")
    if guardrail_id:
        kwargs["guardrail_id"] = guardrail_id
        kwargs["guardrail_version"] = getattr(config, "guardrail_version", "DRAFT")
        kwargs["guardrail_trace"] = "enabled"
        # async, not Bedrock's default sync. In sync mode the guardrail buffers the
        # model's output in scan windows and only releases after each scan, which
        # held the first visible token back ~9s for a long answer (measured raw:
        # sync TTFT 9.4s vs async 1.8s on Haiku) and made the reply arrive in
        # bursts — the "한참 침묵 후 한꺼번에" the user saw. async streams tokens
        # immediately and scans in parallel; Strands still redacts mid-stream on a
        # block. The tradeoff is a brief window where flagged output can appear
        # before redaction, which is acceptable for this platform's guardrail use.
        kwargs["guardrail_stream_processing_mode"] = "async"

    # Prompt caching for the static prefix. The system prompt and the tool schemas
    # repeat verbatim on every turn, so a cachePoint after each lets Bedrock read
    # them back at ~1/10 the input rate instead of re-billing them — which is the
    # whole point of the cache-read tier the insights page counts
    # (`cacheReadInputTokens`), and why it was reading 0 until now: nothing put a
    # cachePoint in the request. Strands inserts those markers from these two
    # fields (`strands.models.bedrock`).
    #
    # Gated to Anthropic models on purpose. A cachePoint on a model that does not
    # support prompt caching is a Converse *validation error*, not a no-op — so we
    # only enable it where we know it is accepted (every model this platform runs is
    # Claude). When the prefix is below the model's per-checkpoint minimum
    # (Haiku 4.5: 4,096 tokens; Sonnet 4.5: 1,024) Bedrock silently skips the cache
    # and the call still succeeds, so there is nothing to size-check here — a small
    # agent simply gets no caching, a large one does.
    if getattr(config, "prompt_cache", False) and "anthropic" in final_model_id.lower():
        kwargs["cache_prompt"] = "default"
        kwargs["cache_tools"] = "default"

    # Anthropic extended thinking, off unless a budget is set (REASONING_BUDGET).
    # Strands has no dedicated thinking field, so it rides additional_request_fields
    # into Converse's additionalModelRequestFields, which is where Claude reads
    # {"thinking": {...}}. The model then streams its reasoning before the answer:
    # the frontend already renders those reasoningContent deltas, so the formerly
    # silent pre-first-token wait becomes visible thinking. It adds tokens and can
    # raise total latency — it does not lower it.
    reasoning_budget = getattr(config, "reasoning_budget", 0) or 0
    if reasoning_budget > 0:
        budget = max(reasoning_budget, MIN_REASONING_BUDGET)
        # budget_tokens must be strictly below max_tokens (thinking and the answer
        # share the output budget); keep room for the answer if they collide.
        ceiling = kwargs.get("max_tokens") or 8192
        if budget >= ceiling:
            budget = max(MIN_REASONING_BUDGET, ceiling - MIN_REASONING_BUDGET)
            kwargs["max_tokens"] = budget + MIN_REASONING_BUDGET
        kwargs["additional_request_fields"] = thinking_fields(final_model_id, budget)
        # Extended thinking requires temperature 1.0; top_p must be left unset
        # (this builder never sets it), so nothing else needs changing.
        kwargs["temperature"] = 1.0

    return BedrockModel(**kwargs)


# A builder takes (config, model, system_prompt, mcp_manager=, session_scope=) and
# returns an AgentRunner.
AgentBuilder = Callable[..., AgentRunner]
