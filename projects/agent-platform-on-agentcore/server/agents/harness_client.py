"""
AgentCore Managed Agent Harness client.

A harness runs the agent loop on the AWS side, so it is invoked through
InvokeHarness rather than InvokeAgentRuntime — its companion runtime explicitly
rejects direct invocation. Unlike InvokeAgentRuntime, InvokeHarness returns a
decoded boto3 event stream rather than SSE bytes.
"""
import logging
import traceback
from typing import Any, AsyncIterator, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from agents.agentcore_client import iter_blocking_stream, stream_boto_config
from agents.agent_config import AgentConfig
from agents.base import AgentClient
from agents.formatters.event_formatter import EventFormatter
from agents.harness_event_adapter import HarnessEventAdapter
from agents.message_utils import MessageUtils
from models.harness import HARNESS_MAX_TOKENS

logger = logging.getLogger(__name__)


class HarnessClient(AgentClient):
    """Client for agents backed by an AgentCore managed harness."""

    def __init__(
        self,
        harness_arn: Optional[str] = None,
        qualifier: Optional[str] = None,
        region_name: Optional[str] = None,
    ):
        self.harness_arn = harness_arn or ""
        self.qualifier = qualifier
        self.region_name = AgentConfig.get_region(region_name)
        # Same streaming-gap reasoning as the runtime client: a harness pauses
        # while its AWS-side tools run, and the default 60s read timeout would
        # sever a long turn.
        self.agentcore_client = boto3.client(
            "bedrock-agentcore",
            region_name=self.region_name,
            config=stream_boto_config(),
        )

    @staticmethod
    def _latest_user_message(values: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Only the newest user message.

        The harness reloads prior turns from AgentCore Memory before it reasons,
        so resending the transcript would duplicate what it already has.
        """
        messages = values.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("type") in ("human", "user"):
                return MessageUtils.build_strands_conversation([msg])
        return []

    async def execute_stream(
        self,
        thread_id: str,
        values: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
        actor_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        harness_arn = (config.get("harness_arn") if config else None) or self.harness_arn
        if not harness_arn:
            yield EventFormatter.error("Harness ARN is required to invoke this agent.")
            return

        messages = self._latest_user_message(values)
        if not messages:
            yield EventFormatter.error("No messages provided")
            return

        qualifier = (config.get("qualifier") if config else None) or self.qualifier

        # Reuse the runtime session id derivation: harness has the same 33-128
        # character requirement, and one session per thread keeps the microVM warm.
        from agents.agentcore_client import AgentCoreClient

        invoke_params: Dict[str, Any] = {
            "harnessArn": harness_arn,
            "runtimeSessionId": AgentCoreClient._session_id(thread_id),
            "messages": messages,
        }
        # Memory events are scoped by actorId + sessionId. Without an actor every
        # caller shares one memory scope; an empty string is rejected outright, so
        # omit the key when the identity is unknown.
        if actor_id:
            invoke_params["actorId"] = actor_id
        if qualifier and qualifier != "DEFAULT":
            invoke_params["qualifier"] = qualifier

        # Per-turn overrides. InvokeHarness replaces `model`/`systemPrompt` for
        # this request only — no new harness version — so a thread can try the
        # same agent on another model or with a different prompt. A cleared form
        # field arrives as "" and means "no override". The model block is
        # swapped wholesale, so the per-call output cap has to ride along or the
        # override falls back to Bedrock's 4096 default (see HARNESS_MAX_TOKENS).
        system_prompt = (config.get("system_prompt") or "").strip() if config else ""
        if system_prompt:
            invoke_params["systemPrompt"] = [{"text": system_prompt}]
        model_id = (config.get("model_id") or "").strip() if config else ""
        if model_id:
            invoke_params["model"] = {
                "bedrockModelConfig": {
                    "modelId": model_id,
                    "maxTokens": HARNESS_MAX_TOKENS,
                }
            }

        # A harness cannot carry binary: InvokeHarness's content block has only
        # text/toolUse/toolResult/reasoningContent. The UI disables attachments for
        # harness agents, so this only fires for a stale client.
        for msg in values.get("messages", []):
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "attachment" for b in content
            ):
                logger.warning(
                    "Dropping attachment(s): a harness cannot receive binary content."
                )
                break

        adapter = HarnessEventAdapter()

        try:
            logger.info(
                "Invoking harness %s (qualifier=%s) with %d message(s)",
                harness_arn,
                qualifier,
                len(messages),
            )
            response = self.agentcore_client.invoke_harness(**invoke_params)

            # Off the loop's thread: a harness is silent while its tools run
            # inside AWS, and iterating this blocking stream directly would hold
            # the event loop for that whole gap — stalling every other request in
            # the process. See iter_blocking_stream.
            async for raw_event in iter_blocking_stream(response["stream"]):
                for event in adapter.adapt(raw_event):
                    yield event

            # Mid-loop stops are suppressed, so a stream that ends on one would
            # otherwise leave the message unpersisted and its spinner running.
            if adapter.started and not adapter.stopped:
                yield adapter.message_stop("end_turn")

        except ClientError as e:
            logger.error(f"Harness ClientError: {e}")
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            yield EventFormatter.error(
                error_message=f"Harness API error ({error_code}): {error_msg}",
                code=error_code,
            )
        except Exception as e:
            logger.error(f"Unexpected error in HarnessClient: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            yield EventFormatter.error(f"Unexpected error: {str(e)}")

        yield EventFormatter.end("completed")
