"""
AgentCore Runtime client for connecting to AgentCore Runtime agents
AgentCore events are passed through as-is, similar to Strands Agent SDK
"""
import json
import boto3
import asyncio
import hashlib
import logging
import traceback
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, AsyncIterator, Optional, List, Tuple
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from agents.base import AgentClient
from agents.agent_config import AgentConfig
from agents.formatters.event_formatter import EventFormatter

logger = logging.getLogger(__name__)

# read_timeout applies to the gap between chunks of a streaming body, not to the
# whole turn. botocore's 60s default is shorter than a single silent step: an
# agents-as-tools runtime blocks inside a @tool wrapper for 20-60s per sub-agent
# and emits nothing meanwhile, so a multi-step answer is cut off mid-stream and
# the partial text is discarded. AgentCore's own ceiling is 15 minutes.
#
# Retries stay off: a retried InvokeAgentRuntime re-runs the whole turn, which
# bills twice and duplicates any tool side effects, and the caller has already
# received part of the stream.
_STREAM_READ_TIMEOUT = 900


def stream_boto_config() -> BotoConfig:
    """A fresh streaming config per client.

    Deliberately not a shared module-level constant: building a client
    normalises the config's `retries` dict in place (`max_attempts: 0` becomes
    `total_max_attempts: 1`), so a shared instance is mutated by whoever
    constructs a client first.
    """
    return BotoConfig(
        read_timeout=_STREAM_READ_TIMEOUT,
        connect_timeout=30,
        retries={"max_attempts": 0, "mode": "standard"},
    )


# Threads for the blocking stream reads below. Its own pool, not asyncio's
# default executor: one read parks a thread for the whole turn, so streams would
# otherwise crowd out every other `run_in_executor` caller (and each other) at
# the default ceiling of min(32, cpu+4). Sized for concurrent chats rather than
# for CPU, since these threads only ever wait on a socket.
#
# FastAPI's own threadpool, which runs the sync routes, is anyio's and is
# separate — a stream cannot starve `GET /threads` by occupying this one.
_STREAM_READ_THREADS = 64
_stream_executor = ThreadPoolExecutor(
    max_workers=_STREAM_READ_THREADS, thread_name_prefix="agentcore-stream"
)

# Ends the iteration below. Not None: botocore yields empty lines as keep-alives
# and one of those must not read as the end of the stream.
_STREAM_END = object()


async def iter_blocking_stream(blocking_iterable: Any) -> AsyncIterator[Any]:
    """Iterate a blocking stream without holding the event loop.

    botocore's streaming bodies block whichever thread iterates them, and an
    agents-as-tools runtime emits nothing while a sub-agent runs: a step like
    `ask_sql_verified`, which executes several candidate SQL statements, is silent
    for 40-70s. Iterated straight from an `async` generator, that entire gap is
    spent inside a socket read while owning the loop, so *every* request in the
    process stalls — not just this stream's. Measured against the deployed
    ks_text2sql_agent: a 68s gap made `GET /` (an `async def` returning a literal
    dict, touching no boto3 and no threadpool) take 67.3s, against 1ms idle.

    An `await asyncio.sleep(0)` next to the blocking call does not help. It runs
    only once a frame has arrived — exactly when the loop was already free. During
    the gap there is no await point at all.

    So each step of the iteration is handed to a worker thread. One frame at a
    time, deliberately: a queue would let the reader race ahead of the consumer
    and buffer a whole turn's tokens for no benefit, since the consumer does
    nothing but forward them to SSE. Exceptions surface from the executor future
    on the caller's side, where the existing handlers turn botocore failures into
    error events.
    """
    loop = asyncio.get_running_loop()
    iterator = iter(blocking_iterable)

    while True:
        # next(it, default) rather than catching StopIteration: StopIteration
        # cannot cross a coroutine boundary — it would surface as a RuntimeError
        # instead of ending the loop.
        item = await loop.run_in_executor(
            _stream_executor, next, iterator, _STREAM_END
        )
        if item is _STREAM_END:
            return
        yield item
    # No cleanup clause: an abandoned stream (GeneratorExit at the yield) leaves
    # at most one worker parked in a read it cannot be interrupted out of — a
    # blocking socket read is not cancellable — and that thread ends by itself
    # when botocore's read timeout fires or the body closes. Waiting for it here
    # would hang the handler for the rest of the turn every time a tab is closed.


def _drain(tool_use_ids: Dict[str, List[str]]) -> List[str]:
    """Tool blocks still open, emptied out, in the order they were opened.

    Some tools never report a result under their own name. In the deployed
    ks_text2sql_agent, `ask_sql_verified` stashes its adopted result as
    `ask_sql_specialist` — internally that is who produced it — so a result labelled
    `ask_sql_verified` is never emitted at all; `render_chart` reports into the chart
    bucket and emits no tool result whatever. The UI reads a call with no result as
    still running, so both left a spinner turning for the rest of the turn.

    Whether a result is coming is only knowable once the turn ends, which is why
    this is drained there rather than guessed at per tool name: any runtime may emit
    a call it never answers, and the server is the one place that sees the end.
    """
    pending = [
        tool_use_id
        for ids in tool_use_ids.values()
        for tool_use_id in ids
    ]
    tool_use_ids.clear()
    # Opened-order, which the synthesised ids encode.
    return sorted(pending, key=lambda i: int(i.rsplit("-", 1)[-1]))


def _closing_events(
    tool_use_ids: Dict[str, List[str]],
    *,
    message_id: str,
    accumulated: str,
    with_stop: bool = True,
) -> List[Dict[str, Any]]:
    """Everything a turn owes the UI on its way out, in the order it must arrive.

    A turn can end five ways — `done`, `final`, a runtime's own `messageStop`, the
    stream simply running out, and a failure mid-read — and every one of them leaves
    the same debt: blocks opened but never settled. Attaching the drain to whichever
    ending happened to be under investigation is what let three of the five keep
    shipping spinners, so the closing sequence lives here once and each ending
    delegates to it.

    Results come before the stop because the UI closes the message when it applies
    `messageStop`; a result after that point is dropped on the floor.

    `with_stop=False` is for the runtime's own `messageStop`, which is forwarded
    verbatim — the drain still has to precede it, but a second stop would have the
    UI close the message twice.
    """
    events: List[Dict[str, Any]] = [
        EventFormatter.tool_result(tool_use_id=tool_use_id, result=None)
        for tool_use_id in _drain(tool_use_ids)
    ]
    if with_stop:
        events.append(
            EventFormatter.message_stop(
                message_id=message_id,
                full_text=accumulated,
                stop_reason="end_turn",
            )
        )
    return events


def _json_default(value: Any) -> Any:
    """Base64 raw bytes so a content block survives json.dumps.

    The SDK would accept bytes directly, but this payload is serialised to JSON
    for InvokeAgentRuntime, so the runtime decodes it on the far side.
    """
    if isinstance(value, (bytes, bytearray)):
        import base64

        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


class AgentCoreClient(AgentClient):
    """
    Client for AgentCore Runtime agents using InvokeAgentRuntime API
    
    AgentCore returns events with "event" field that are passed through as-is.
    Similar to Strands Agent SDK - no complex conversion needed.
    """

    def __init__(
        self,
        agent_runtime_arn: Optional[str] = None,
        qualifier: Optional[str] = None,
        region_name: Optional[str] = None,
        attachment_service: Optional[Any] = None,
    ):
        # Allow initialization without ARN - will be validated when actually used
        self.agent_runtime_arn = AgentConfig.get_agent_runtime_arn(agent_runtime_arn)
        self.qualifier = AgentConfig.get_qualifier(qualifier)
        self.region_name = AgentConfig.get_region(region_name)
        self.attachment_service = attachment_service

        # Initialize Bedrock AgentCore client
        self.agentcore_client = boto3.client(
            "bedrock-agentcore",
            region_name=self.region_name,
            config=stream_boto_config(),
        )

    @staticmethod
    def _split_content(message: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        """Separate a message's text from its attachment references."""
        content = message.get("content", "")
        if isinstance(content, str):
            return content, []

        texts: List[str] = []
        refs: List[Dict[str, Any]] = []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "attachment":
                    refs.append(block)
                elif block.get("text"):
                    texts.append(block["text"])
        return "".join(texts), refs

    @staticmethod
    def _describe(refs: List[Dict[str, Any]]) -> str:
        """Neutral prompt text for an attachment-only send.

        A document block requires accompanying text and an empty string is not
        accompaniment, so say plainly what arrived.
        """
        names = [r.get("filename") or "a file" for r in refs]
        joined = ", ".join(names)
        if len(refs) == 1:
            return f"Please look at the attached file: {joined}"
        return f"Please look at the attached files: {joined}"

    def _prepare_payload(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        actor_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        skip_recall: bool = False,
        model_id: Optional[str] = None,
    ) -> bytes:
        """
        Prepare JSON payload for AgentCore Runtime.

        Only the newest user message travels: the runtime restores prior turns
        from AgentCore Memory, scoped by actor_id plus its session id.

        With no attachments the prompt stays a plain string, byte-identical to
        before this feature — a runtime deployed earlier keeps serving ordinary
        chats, and only attachment sends need the newer one.
        """
        last_human = None
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("type") in ("human", "user"):
                last_human = msg
                break
        if not last_human:
            raise ValueError("No user message found")

        text, refs = self._split_content(last_human)

        blocks: List[Dict[str, Any]] = []
        if refs and self.attachment_service is not None and thread_id:
            blocks = self.attachment_service.model_blocks(thread_id, refs)

        payload_dict: Dict[str, Any] = {}
        if blocks:
            # Text first: Bedrock requires it beside a document block, and its own
            # examples lead with it.
            payload_dict["prompt"] = [{"text": text or self._describe(refs)}] + blocks
        else:
            payload_dict["prompt"] = text

        if system_prompt:
            payload_dict["system_prompt"] = system_prompt
        if actor_id:
            payload_dict["actor_id"] = actor_id
        # Only sent when true: a runtime deployed before this key ignores it, and
        # its absence means "recall as before", so an old runtime and a continuing
        # turn behave identically to how they did.
        if skip_recall:
            payload_dict["skip_recall"] = True
        # The runtime reads `model_id` from the payload and falls back to its own
        # MODEL_ID when absent (agent-runtime/main.py), so a turn without an
        # override is byte-identical to before.
        if model_id:
            payload_dict["model_id"] = model_id

        return json.dumps(payload_dict, default=_json_default).encode("utf-8")
    
    @staticmethod
    def _session_id(thread_id: str) -> str:
        """
        Derive a runtimeSessionId from a thread id.

        AgentCore requires 33-128 characters, while thread ids are shorter, so
        short ids are padded deterministically to keep one session per thread.
        """
        session_id = "".join(c if c.isalnum() or c in "-_" else "-" for c in thread_id)
        if len(session_id) < 33:
            digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
            session_id = f"{session_id}-{digest}"[:128]
        return session_id[:128]

    def _parse_sse_event(self, data_str: str) -> Optional[Dict[str, Any]]:
        """
        Parse SSE event data string
        
        Args:
            data_str: Data string from SSE event
            
        Returns:
            Parsed event dictionary or None if invalid
        """
        try:
            return json.loads(data_str)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse SSE event: {e}")
            return None
    
    async def _process_sse_stream(
        self,
        response_stream: Any
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Process SSE stream from AgentCore and normalize it to Strands event format.

        Deployed runtimes emit one of three shapes:
          1. Strands-native: {"event": {"contentBlockDelta": {...}}} — passed through.
          2. Simplified:     {"type": "text_delta", "delta": "..."} followed by
             {"type": "final", "response": "...", "usage": {...}} — converted here.
          3. Typed:          {"type": "text", "chunk": "..."} plus reasoning /
             tool_call / tool_result / done — what an async-generator entrypoint on
             BedrockAgentCoreApp produces, since the SDK frames each yielded dict
             verbatim. Also converted here.

        An unrecognised frame is dropped, and dropping every frame of a turn is
        worse than an error: `started` never flips, so no messageStart and no
        synthetic messageStop are emitted either, and the UI shows neither an
        answer nor a failure. So shape 3 is translated rather than skipped.

        Args:
            response_stream: Response stream from AgentCore

        Yields:
            Events in Strands Agent format
        """
        message_id = f"msg-{int(time.time() * 1000)}"
        accumulated = ""
        started = False
        stopped = False
        # Typed tool_call frames carry no toolUseId, but the UI needs one to match
        # a result to its call. Ids are synthesised per tool name, and the block
        # index is bumped so tool blocks never collide with the text block at 0.
        #
        # A *queue* per name, not one id per name: an orchestrator can open two
        # calls to the same tool before either returns (a multi-part question does
        # exactly that), and a single slot let the second overwrite the first —
        # leaving the first block with no result, which the UI renders as a spinner
        # that never stops. Results settle the oldest open call of that name, which
        # is also the order the runtime drains its stash in.
        tool_use_ids: Dict[str, List[str]] = {}
        next_block_index = 1

        try:
            # chunk_size=1 keeps botocore from buffering, so a frame reaches the
            # UI as soon as the runtime emits it. The read itself runs on a worker
            # thread — see iter_blocking_stream for why that is load-bearing.
            async for line in iter_blocking_stream(
                response_stream.iter_lines(chunk_size=1)
            ):
                if not line:
                    continue

                line_str = line.decode('utf-8').strip()
                if not line_str or line_str.startswith(":"):
                    continue

                if not line_str.startswith("data: "):
                    continue

                data_str = line_str[6:].strip()

                # Parse event data
                event_data = self._parse_sse_event(data_str)
                if not isinstance(event_data, dict):
                    continue

                if "event" in event_data:
                    inner = event_data.get("event")
                    if isinstance(inner, dict):
                        if "messageStart" in inner:
                            started = True
                        # A native messageStop terminates the turn; without this the
                        # synthetic stop below would emit a second, empty one.
                        if "messageStop" in inner:
                            stopped = True
                            # Settle first: this stop is about to close the message,
                            # and the runtime that sent it is under no obligation to
                            # have answered every call it opened.
                            for event in _closing_events(
                                tool_use_ids,
                                message_id=message_id,
                                accumulated=accumulated,
                                with_stop=False,
                            ):
                                yield event
                    yield event_data
                    continue

                event_type = event_data.get("type")

                if event_type == "text_delta":
                    delta = event_data.get("delta", "")
                    if not delta:
                        continue
                    if not started:
                        started = True
                        yield EventFormatter.message_start(message_id)
                    accumulated += delta
                    yield EventFormatter.content_delta(
                        text=delta,
                        accumulated=accumulated,
                        message_id=message_id,
                        content_block_index=0,
                    )

                elif event_type == "final":
                    # Some runtimes only send `final` with the whole response and no
                    # incremental deltas; emit the text so nothing is lost.
                    final_text = event_data.get("response", "")
                    if not started:
                        started = True
                        yield EventFormatter.message_start(message_id)
                    if final_text and not accumulated:
                        accumulated = final_text
                        yield EventFormatter.content_delta(
                            text=final_text,
                            accumulated=accumulated,
                            message_id=message_id,
                            content_block_index=0,
                        )

                    usage = event_data.get("usage") or {}
                    if usage:
                        yield EventFormatter.metadata(
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            total_tokens=usage.get("total_tokens", 0),
                        )

                    stopped = True
                    for event in _closing_events(
                        tool_use_ids,
                        message_id=message_id,
                        accumulated=accumulated,
                    ):
                        yield event

                elif event_type == "text":
                    chunk = event_data.get("chunk", "")
                    if not chunk:
                        continue
                    if not started:
                        started = True
                        yield EventFormatter.message_start(message_id)
                    accumulated += chunk
                    yield EventFormatter.content_delta(
                        text=chunk,
                        accumulated=accumulated,
                        message_id=message_id,
                        content_block_index=0,
                    )

                elif event_type == "reasoning":
                    chunk = event_data.get("chunk", "")
                    if not chunk:
                        continue
                    if not started:
                        started = True
                        yield EventFormatter.message_start(message_id)
                    # Its own lane: reasoning must not land in the answer body, so
                    # it is deliberately left out of `accumulated`.
                    yield EventFormatter.reasoning_delta(
                        text=chunk,
                        accumulated=chunk,
                        message_id=message_id,
                        content_block_index=0,
                    )

                elif event_type == "tool_call":
                    tool_name = event_data.get("tool") or "tool"
                    if not started:
                        started = True
                        yield EventFormatter.message_start(message_id)
                    tool_use_id = f"{message_id}-tool-{next_block_index}"
                    tool_use_ids.setdefault(tool_name, []).append(tool_use_id)
                    yield {
                        "event": {
                            "contentBlockStart": {
                                "contentBlockIndex": next_block_index,
                                "start": {
                                    "toolUse": {
                                        "toolUseId": tool_use_id,
                                        "name": tool_name,
                                    }
                                },
                            }
                        }
                    }
                    tool_args = event_data.get("args")
                    if tool_args:
                        # Strands-native toolUse.input is a *string* — the SDK
                        # streams tool arguments as partial JSON text, and every
                        # consumer downstream accumulates it with `+`: the server
                        # (streaming_service, current_tool_uses[...]["input"] +=)
                        # and the browser (useStream, currentInput + toolInput).
                        # A typed `tool_call` frame carries `args` as an already
                        # assembled dict; forwarding it verbatim made the server
                        # do `"" + dict` (TypeError, surfaced as an error card)
                        # and the browser do `"" + {}` = "[object Object]", which
                        # then failed JSON.parse. Serialise here so the block
                        # matches the string contract. ensure_ascii=False keeps
                        # Korean argument values legible.
                        if not isinstance(tool_args, str):
                            tool_args = json.dumps(tool_args, ensure_ascii=False)
                        yield {
                            "event": {
                                "contentBlockDelta": {
                                    "contentBlockIndex": next_block_index,
                                    "delta": {"toolUse": {"input": tool_args}},
                                }
                            }
                        }
                    next_block_index += 1

                elif event_type == "tool_result":
                    tool_name = event_data.get("tool") or "tool"
                    # Oldest open call of this name, removed as it is settled: each
                    # result belongs to one call, and the frames carry no id to pair
                    # on. FIFO is the only stable choice, and it matches the order
                    # the runtime flushes its stashed results in.
                    pending = tool_use_ids.get(tool_name)
                    tool_use_id = pending.pop(0) if pending else None
                    if not tool_use_id:
                        # A sub-agent invoked inside another tool produces a result
                        # with no call of its own — the orchestrator stream never
                        # showed a toolUse for it. The UI can only attach a result
                        # to a block it has seen, so open one now and settle it
                        # immediately: dropping it would discard work the runtime
                        # actually did and reported.
                        if not started:
                            started = True
                            yield EventFormatter.message_start(message_id)
                        tool_use_id = f"{message_id}-tool-{next_block_index}"
                        yield {
                            "event": {
                                "contentBlockStart": {
                                    "contentBlockIndex": next_block_index,
                                    "start": {
                                        "toolUse": {
                                            "toolUseId": tool_use_id,
                                            "name": tool_name,
                                        }
                                    },
                                }
                            }
                        }
                        next_block_index += 1
                    yield EventFormatter.tool_result(
                        tool_use_id=tool_use_id,
                        result=event_data.get("result"),
                    )

                elif event_type == "done":
                    # Terminates the turn. A prewarm ping sends only `done`, so a
                    # turn that never started must not be given a message here.
                    if started and not stopped:
                        stopped = True
                        for event in _closing_events(
                            tool_use_ids,
                            message_id=message_id,
                            accumulated=accumulated,
                        ):
                            yield event

                elif event_type in ("thinking", "heartbeat"):
                    # Liveness frames: what the agent is doing while it emits no
                    # text. Surfaced as transient status so a long silent step
                    # does not look like a hang.
                    #
                    # Deliberately does NOT touch `started`/`accumulated`: status
                    # is not content, so it must not open an assistant message.
                    # A prewarm ping would otherwise leave an empty bubble.
                    label = event_data.get("label") or event_data.get("text")
                    if not label:
                        continue
                    yield EventFormatter.agent_status(
                        label=label,
                        phase=event_data.get("phase"),
                        elapsed_ms=event_data.get("elapsed_ms"),
                    )

                elif event_type == "chart":
                    # A chart the runtime drew. Deliberately does not touch
                    # `started`/`accumulated`: a chart is not message content, so it
                    # must not open an assistant message nor land in the saved prose.
                    #
                    # The orchestrator also writes the image URL into the answer as
                    # markdown, which is why charts appeared even while this frame
                    # was dropped — but that URL is presigned for five minutes and
                    # the object outlives it by days, so a reopened thread showed a
                    # broken image. Forwarding `spec` is what makes the chart
                    # durable; the image rides along for the sandbox path, which has
                    # no spec.
                    spec = event_data.get("spec")
                    url = event_data.get("url")
                    if spec is None and not url:
                        # Nothing to draw and nothing the browser can load. The
                        # sandbox path lands here: it reports only an `s3://` URI,
                        # which is not a URL, and there is no presign route for the
                        # runtime's staging bucket — so the frame is dropped rather
                        # than rendered as a card with nothing in it.
                        continue
                    yield EventFormatter.chart(
                        spec=spec,
                        url=url,
                        source=event_data.get("source"),
                    )

                elif event_type == "verification":
                    # Evidence for the answer's numbers: how many candidate queries
                    # were executed and how far they agreed. Advisory, so a frame
                    # with no verdict is dropped rather than rendered as an empty
                    # card.
                    result = event_data.get("result")
                    if not isinstance(result, dict) or not result.get("verdict"):
                        continue
                    yield EventFormatter.verification(result)

                elif event_type == "validator":
                    # Advisory grounding result. Not progress and not an answer;
                    # nothing in the UI consumes it yet.
                    continue

                elif event_type == "error" or "error" in event_data:
                    yield EventFormatter.error(
                        str(event_data.get("error") or event_data.get("message") or event_data)
                    )

                else:
                    logger.debug(f"Skipping unrecognized AgentCore event: {event_data}")

            # Runtimes that stream deltas without a terminating `final` still need a
            # messageStop for the message to be persisted.
            if started and not stopped:
                # A truncated turn must not leave the spinners it opened either.
                stopped = True
                for event in _closing_events(
                    tool_use_ids,
                    message_id=message_id,
                    accumulated=accumulated,
                ):
                    yield event

        except Exception as stream_error:
            logger.error(f"Error reading AgentCore stream: {stream_error}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            yield EventFormatter.error(f"Stream reading error: {str(stream_error)}")
            # The error alone settles nothing: the UI attaches results and closes the
            # message on `messageStop`, which this path never sent — so a call still
            # open here spins forever, and the partial answer was never persisted.
            # This is the only ending with no second line of defence, since the
            # browser's own fallback also hangs off `messageStop`.
            #
            # A failure before the turn produced anything is left alone: there is no
            # message to close, and opening one would leave an empty bubble.
            if started and not stopped:
                stopped = True
                for event in _closing_events(
                    tool_use_ids,
                    message_id=message_id,
                    accumulated=accumulated,
                ):
                    yield event
    
    async def execute_stream(
        self,
        thread_id: str,
        values: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
        actor_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Execute agent workflow using AgentCore Runtime InvokeAgentRuntime API

        AgentCore events are passed through as-is (similar to Strands Agent SDK).
        """
        messages = values.get("messages", [])
        if not messages:
            yield EventFormatter.error("No messages provided")
            return

        # Get agent runtime ARN and qualifier from config if provided
        agent_runtime_arn = config.get("agent_runtime_arn") if config else None
        if not agent_runtime_arn:
            agent_runtime_arn = self.agent_runtime_arn
        
        qualifier = config.get("qualifier") if config else None
        if qualifier is None:
            qualifier = self.qualifier

        # Validate ARN is provided before making request
        if not agent_runtime_arn:
            yield EventFormatter.error("Agent Runtime ARN is required. Please configure it in settings.")
            return

        try:
            # Only forward a system prompt when one is explicitly configured: the
            # runtime falls back to its own ORCHESTRATOR_PROMPT, and sending a
            # generic default here would silently override it.
            system_prompt = config.get("system_prompt") if config else None

            # Prepare payload for AgentCore
            # Server-set on a thread's first turn (see StreamingService): the
            # runtime session has no prior events, so recall would read Memory
            # only to restore nothing. Rides in config rather than as its own
            # argument so the AgentClient.execute_stream signature stays put.
            skip_recall = bool(config.get("skip_recall")) if config else False

            # Per-turn model, set by the server for basic chat. Harness overrides
            # travel the same key, so a runtime agent given one honours it too.
            model_id = config.get("model_id") if config else None

            try:
                payload = self._prepare_payload(
                    messages, system_prompt, actor_id, thread_id, skip_recall,
                    model_id=model_id,
                )
            except ValueError as e:
                yield EventFormatter.error(str(e))
                return

            # Prepare invoke_agent_runtime parameters
            invoke_params = {
                "agentRuntimeArn": agent_runtime_arn,
                "runtimeSessionId": self._session_id(thread_id),
                "payload": payload,
            }

            # Add qualifier if provided
            if qualifier:
                invoke_params["qualifier"] = qualifier

            # Invoke AgentCore Runtime - this returns a streaming response
            logger.info(f"Invoking AgentCore with ARN: {agent_runtime_arn}, qualifier: {qualifier}")

            response = self.agentcore_client.invoke_agent_runtime(**invoke_params)

            # Check content type
            content_type = response.get("contentType", "")
            logger.info(f"AgentCore response content type: {content_type}")
            
            if "text/event-stream" in content_type:
                # Handle streaming SSE response
                response_stream = response.get("response")
                if not response_stream:
                    yield EventFormatter.error("No response stream from AgentCore")
                    return
                
                # Process SSE stream - pass through events as-is
                async for event in self._process_sse_stream(response_stream):
                    yield event
            
            elif content_type == "application/json":
                # Handle standard JSON response (non-streaming)
                response_body = response.get("response", [])
                response_text = ''.join([part.decode('utf-8') for part in response_body])
                response_data = json.loads(response_text)
                
                # If response has "event" field, pass through as-is
                if isinstance(response_data, dict) and "event" in response_data:
                    yield response_data
                else:
                    # Convert plain response to event format
                    message_id = f"msg-{int(time.time() * 1000)}"
                    yield EventFormatter.message_start(message_id)
                    
                    text_content = response_data.get("text", str(response_data)) if isinstance(response_data, dict) else str(response_data)
                    
                    yield EventFormatter.content_delta(
                        text=text_content,
                        accumulated=text_content,
                        message_id=message_id,
                        content_block_index=0,
                    )
                    
                    yield EventFormatter.message_stop(
                        message_id=message_id,
                        full_text=text_content,
                        stop_reason="end_turn",
                    )
            else:
                # Unknown content type
                logger.warning(f"Unknown content type from AgentCore: {content_type}")
                yield EventFormatter.error(f"Unknown response format: {content_type}")

        except ClientError as e:
            logger.error(f"AgentCore ClientError: {e}")
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            yield EventFormatter.error(
                error_message=f"AgentCore API error ({error_code}): {error_msg}",
                code=error_code
            )
        except Exception as e:
            logger.error(f"Unexpected error in AgentCoreClient: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            yield EventFormatter.error(f"Unexpected error: {str(e)}")
        
        # Send end event
        yield EventFormatter.end("completed")
