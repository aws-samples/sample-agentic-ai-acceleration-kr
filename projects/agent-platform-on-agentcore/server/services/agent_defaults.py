"""What an agent runs with when a thread overrides nothing.

The override popover lets a user swap the model or replace the system prompt
for one thread, but until now it showed neither baseline: the model item read
"agent default" and the prompt box was empty, so the user had to guess what
they were replacing. This answers that question from the deployed definition,
never from a copy the server keeps, so it cannot drift from what runs.

What can be known:
- A harness carries both in its definition (GetHarness: `model`, `systemPrompt`).
- A runtime carries only its model, in the `MODEL_ID` environment variable. Its
  prompt is a constant inside the container (agent-runtime/agents/*.py) and no
  API reads it, so `system_prompt` stays None and the UI says so.

The model comes from `UsageService.resolve_model_for`, the same resolver the
ledger prices turns with, so the popover's "default" and the model a turn is
billed under can never disagree. It also resolves an inference-profile ARN to
the model it routes to, and caches per ARN.
"""
import logging
from typing import Any, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AgentDefaults(BaseModel):
    """None means "not knowable", not "empty" — the UI shows these differently."""

    model_id: Optional[str] = None
    system_prompt: Optional[str] = None


def resolve_agent_defaults(record: Any, *, usage: Any, harness: Any) -> AgentDefaults:
    harness_arn = getattr(record, "harness_arn", None) or None
    runtime_arn = getattr(record, "agent_runtime_arn", None) or None
    if not harness_arn and not runtime_arn:
        return AgentDefaults()

    model_id = usage.resolve_model_for(harness_arn=harness_arn, agent_runtime_arn=runtime_arn)

    system_prompt: Optional[str] = None
    if harness_arn:
        # GetHarness takes the id, the last path segment of the ARN.
        harness_id = harness_arn.rsplit("/", 1)[-1]
        try:
            system_prompt = getattr(harness.get_harness(harness_id), "system_prompt", None)
        except Exception:
            # A failed read leaves the prompt unknown; the model still answers.
            logger.warning("Could not read harness %s for its prompt", harness_id, exc_info=True)

    return AgentDefaults(model_id=model_id, system_prompt=system_prompt)
