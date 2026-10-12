"""
Normalizes InvokeHarness events into the Strands event format the platform uses.

InvokeHarness emits Bedrock Converse-shaped events, which are close to what the UI
already parses but differ in two ways that must be reconciled:

  1. Tool results arrive as their own `messageStart{role: "user"}` turn. The UI and
     the persistence loop both start a fresh assistant message on every
     `messageStart`, so passing these through produces empty assistant bubbles and
     splits one logical answer across several messages. Those turns are suppressed
     and only their tool results are forwarded.
  2. Tool results are carried in `contentBlockDelta.delta.toolResult` as a list of
     content blocks, whereas the UI reads a flat `toolResult{toolUseId, result}`
     event. The block index is mapped back to its toolUseId to translate. Those
     blocks are *deltas* — one result can span many events — so they are
     accumulated here and every event carries the whole result so far, along with
     the tool's success/error verdict, which only the block start reports.
"""
import json
import time
from typing import Any, Dict, List, Optional, Tuple


class HarnessEventAdapter:
    """Stateful per-invocation translator. Not safe to share across streams."""

    def __init__(self, message_id: Optional[str] = None):
        self._base_id = message_id or f"msg-{int(time.time() * 1000)}"
        self._turn = 0
        self._message_id: Optional[str] = None
        # True while inside a tool-result turn emitted with role="user".
        self._suppressing = False
        self._started = False
        self._stopped = False
        self._text = ""
        # contentBlockIndex -> toolUseId, for correlating toolResult deltas.
        self._tool_use_by_index: Dict[int, str] = {}
        # contentBlockIndex -> the result text accumulated so far, one entry per
        # position in the result's own content list.
        self._tool_result_parts: Dict[int, List[str]] = {}
        # contentBlockIndex -> the tool's own verdict, which arrives on the block
        # start and has to be restamped on every delta that follows.
        self._tool_result_status: Dict[int, str] = {}

    @property
    def message_id(self) -> str:
        if self._message_id is None:
            self._message_id = self._base_id
        return self._message_id

    @property
    def started(self) -> bool:
        return self._started

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def accumulated_text(self) -> str:
        return self._text

    def adapt(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Translate one raw harness event into zero or more platform events."""
        if not isinstance(event, dict):
            return []

        if "messageStart" in event:
            return self._on_message_start(event["messageStart"] or {})
        if "contentBlockStart" in event:
            return self._on_content_block_start(event["contentBlockStart"] or {})
        if "contentBlockDelta" in event:
            return self._on_content_block_delta(event["contentBlockDelta"] or {})
        if "contentBlockStop" in event:
            return self._on_content_block_stop(event["contentBlockStop"] or {})
        if "messageStop" in event:
            return self._on_message_stop(event["messageStop"] or {})
        if "metadata" in event:
            return self._on_metadata(event["metadata"] or {})

        error = self._error_message(event)
        if error is not None:
            return [{"event": {"error": {"error": error}}}]

        return []

    # --- handlers ----------------------------------------------------------

    def _on_message_start(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if payload.get("role") == "user":
            # A tool-result turn, not a new assistant message.
            self._suppressing = True
            return []

        self._suppressing = False
        if self._started:
            # The agent loop continues the same logical answer after running tools,
            # so keep appending to the message the UI already rendered.
            return []

        self._started = True
        self._turn += 1
        self._message_id = self._base_id
        return [
            {
                "event": {
                    "messageStart": {
                        "id": self.message_id,
                        "role": payload.get("role", "assistant"),
                    }
                }
            }
        ]

    def _on_content_block_start(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        index = payload.get("contentBlockIndex", 0)
        start = payload.get("start") or {}

        tool_result = start.get("toolResult")
        if isinstance(tool_result, dict):
            tool_use_id = tool_result.get("toolUseId")
            if tool_use_id:
                self._tool_use_by_index[index] = tool_use_id
                # Block indexes are per message, so one index serves several tool
                # results over a turn. Anything left from the last occupant would
                # be prepended to this result, or mark it failed on its behalf.
                self._tool_result_parts.pop(index, None)
                self._tool_result_status.pop(index, None)
                # "success" | "error" — the only place the stream says whether the
                # tool actually worked. The deltas that carry the result itself do
                # not repeat it, so it is held here until they arrive.
                #
                # It is the *invocation* that is being judged, not the output.
                # Measured against the deployed test_builtin_harness_agent: code
                # interpreter running `raise RuntimeError(...)` reports "success"
                # and hands back the traceback as its result, while web search
                # called with an empty query reports "error". So this marks calls
                # that never ran, not calls whose output mentions a failure.
                status = tool_result.get("status")
                if status:
                    self._tool_result_status[index] = status
            return []

        tool_use = start.get("toolUse")
        if isinstance(tool_use, dict):
            tool_use_id = tool_use.get("toolUseId")
            name = tool_use.get("name")
            if not tool_use_id or not name:
                return []
            self._tool_use_by_index[index] = tool_use_id
            events = self._ensure_started()
            events.append(
                {
                    "event": {
                        "contentBlockStart": {
                            "contentBlockIndex": index,
                            "start": {
                                "toolUse": {"toolUseId": tool_use_id, "name": name}
                            },
                        }
                    }
                }
            )
            return events

        return []

    def _on_content_block_delta(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        index = payload.get("contentBlockIndex", 0)
        delta = payload.get("delta") or {}

        tool_result = delta.get("toolResult")
        if isinstance(tool_result, list):
            tool_use_id = self._tool_use_by_index.get(index)
            if not tool_use_id:
                return []
            # Forwarded even while suppressing: the surrounding turn is noise but
            # the result itself is what completes the tool call in the UI.
            #
            # The accumulated total is sent every time rather than this delta
            # alone, because both consumers assign rather than append — the
            # browser's `applyToolResult` and this server's own persistence loop.
            # Sending fragments left them holding only the last one, which is a
            # result with its front cut off. Carrying the running total also means
            # a stream that dies mid-result still shows what did arrive.
            settled: Dict[str, Any] = {
                "toolUseId": tool_use_id,
                "result": self._accumulate_tool_result(index, tool_result),
            }
            # Restamped on every event for the same reason the result is: the
            # consumers assign rather than merge, so a later event without it
            # would erase the verdict an earlier one carried. Left out entirely
            # when the stream never said — inventing "success" would put words in
            # the tool's mouth.
            status = self._tool_result_status.get(index)
            if status:
                settled["status"] = status
            return [{"event": {"toolResult": settled}}]

        if self._suppressing:
            return []

        tool_use = delta.get("toolUse")
        if isinstance(tool_use, dict):
            tool_input = tool_use.get("input")
            if not tool_input:
                return []
            events = self._ensure_started()
            events.append(
                {
                    "event": {
                        "contentBlockDelta": {
                            "contentBlockIndex": index,
                            "delta": {"toolUse": {"input": tool_input}},
                        }
                    }
                }
            )
            return events

        reasoning = delta.get("reasoningContent")
        if isinstance(reasoning, dict):
            text = reasoning.get("text")
            if not text:
                return []
            events = self._ensure_started()
            events.append(
                {
                    "event": {
                        "contentBlockDelta": {
                            "contentBlockIndex": index,
                            "delta": {"reasoningContent": {"text": text}},
                        }
                    }
                }
            )
            return events

        text = delta.get("text")
        if text:
            events = self._ensure_started()
            self._text += text
            events.append(
                {
                    "event": {
                        "contentBlockDelta": {
                            "contentBlockIndex": index,
                            "delta": {"text": text},
                        }
                    }
                }
            )
            return events

        return []

    def _on_content_block_stop(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self._suppressing:
            return []
        index = payload.get("contentBlockIndex", 0)
        if index not in self._tool_use_by_index:
            return []
        # Only tool-use blocks need the stop: it tells the UI to parse the
        # accumulated JSON input and settle the tool call.
        return [{"event": {"contentBlockStop": {"contentBlockIndex": index}}}]

    def _on_message_stop(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        stop_reason = payload.get("stopReason")
        # tool_use / tool_result stops are mid-loop: the agent is still working.
        if self._suppressing or stop_reason in ("tool_use", "tool_result"):
            return []
        return [self.message_stop(stop_reason or "end_turn")]

    def _on_metadata(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        usage = payload.get("usage") or {}
        metadata_event = {
            "event": {
                "metadata": {
                    "usage": {
                        "inputTokens": usage.get("inputTokens", 0),
                        "outputTokens": usage.get("outputTokens", 0),
                        "totalTokens": usage.get("totalTokens", 0),
                    }
                }
            }
        }
        # Forward the Bedrock trace if present so the server can record guardrail
        # interventions. The reader expects Bedrock's own nesting
        # (`metadata.trace.guardrail`), so a bare `guardrail` key is re-nested
        # rather than passed through at the top level where nothing reads it.
        if isinstance(payload.get("trace"), dict):
            metadata_event["event"]["metadata"]["trace"] = payload["trace"]
        elif "guardrail" in payload:
            metadata_event["event"]["metadata"]["trace"] = {"guardrail": payload["guardrail"]}
        # Forward cache tokens if present, mirroring the runtime path's forwarding
        # so the server's usage parsing handles both harness and runtime events uniformly.
        if "cacheReadInputTokens" in usage:
            metadata_event["event"]["metadata"]["usage"]["cacheReadInputTokens"] = usage.get(
                "cacheReadInputTokens", 0
            )
        if "cacheWriteInputTokens" in usage:
            metadata_event["event"]["metadata"]["usage"]["cacheWriteInputTokens"] = usage.get(
                "cacheWriteInputTokens", 0
            )
        return [metadata_event]

    # --- helpers -----------------------------------------------------------

    def _accumulate_tool_result(self, index: int, blocks: List[Any]) -> str:
        """Fold one toolResult delta into the block's running result.

        Position in the delta list is position in the tool result's own `content`
        list, so each is accumulated on its own and the pieces are joined for
        display — concatenating across positions would run two separate content
        blocks together into one word.
        """
        parts = self._tool_result_parts.setdefault(index, [])
        for position, block in enumerate(blocks):
            chunk, is_partial = _tool_result_chunk(block)
            while len(parts) <= position:
                parts.append("")
            # Only text streams. `json` is a document type, which cannot hold half
            # a value, so each json delta is already whole and replaces its slot —
            # appending would produce `{...}{...}`, which parses as nothing.
            parts[position] = parts[position] + chunk if is_partial else chunk
        return "\n".join(parts)

    def _ensure_started(self) -> List[Dict[str, Any]]:
        """Emit a messageStart if content arrives before one."""
        if self._started:
            return []
        return self._on_message_start({"role": "assistant"})

    def message_stop(self, stop_reason: str = "end_turn") -> Dict[str, Any]:
        self._stopped = True
        return {
            "event": {
                "messageStop": {
                    "stopReason": stop_reason,
                    "messageId": self.message_id,
                    "fullText": self._text,
                }
            }
        }

    @staticmethod
    def _error_message(event: Dict[str, Any]) -> Optional[str]:
        for key in (
            "runtimeClientError",
            "validationException",
            "internalServerException",
            "throttlingException",
            "accessDeniedException",
            "serviceQuotaExceededException",
            "conflictException",
            "resourceNotFoundException",
        ):
            payload = event.get(key)
            if isinstance(payload, dict):
                return payload.get("message") or key
        return None


def _tool_result_chunk(block: Any) -> Tuple[str, bool]:
    """Render one toolResult delta block as (text, appendable).

    `appendable` says whether the text is a fragment of something longer. Only
    `text` deltas are; everything else is a complete value rendered whole.
    """
    if isinstance(block, dict):
        if isinstance(block.get("text"), str):
            return block["text"], True
        if "json" in block:
            return json.dumps(block["json"], default=str), False
        return json.dumps(block, default=str), False
    if block is None:
        return "", False
    return str(block), False
