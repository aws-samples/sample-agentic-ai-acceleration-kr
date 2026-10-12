"""
Common event formatter for all agent clients
Uses Strands Agent SDK official event format
"""
from typing import Dict, Any, Optional
import time


class EventFormatter:
    """Formats events from different agent modes into Strands Agent SDK format"""
    
    @staticmethod
    def message_start(message_id: Optional[str] = None, role: str = "assistant") -> Dict[str, Any]:
        """Format messageStart event in Strands Agent format"""
        if not message_id:
            message_id = f"msg-{int(time.time() * 1000)}"
        return {
            "event": {
                "messageStart": {
                    "id": message_id,
                    "role": role,
                }
            }
        }
    
    @staticmethod
    def content_delta(
        text: str,
        accumulated: str,
        message_id: str,
        content_block_index: int = 0,
    ) -> Dict[str, Any]:
        """Format contentBlockDelta event in Strands Agent format"""
        return {
            "event": {
                "contentBlockDelta": {
                    "delta": {
                        "text": text,
                    },
                    "contentBlockIndex": content_block_index,
                }
            }
        }
    
    @staticmethod
    def message_stop(
        message_id: str,
        full_text: str,
        stop_reason: str = "stop",
        full_reasoning: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Format messageStop event in Strands Agent format"""
        stop_info: Dict[str, Any] = {
            "stopReason": stop_reason,
            "messageId": message_id,
            "fullText": full_text,
        }
        if full_reasoning:
            stop_info["fullReasoning"] = full_reasoning
        return {
            "event": {
                "messageStop": stop_info
            }
        }
    
    @staticmethod
    def reasoning_delta(
        text: str,
        accumulated: str,
        message_id: str,
        content_block_index: int = 0,
    ) -> Dict[str, Any]:
        """Format reasoning contentBlockDelta event in Strands Agent format"""
        return {
            "event": {
                "contentBlockDelta": {
                    "delta": {
                        "reasoningContent": {
                            "text": text,
                        }
                    },
                    "contentBlockIndex": content_block_index,
                }
            }
        }
    
    @staticmethod
    def metadata(
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Format metadata event in Strands Agent format"""
        metadata_info: Dict[str, Any] = {
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": total_tokens,
            }
        }
        if latency_ms is not None:
            metadata_info["metrics"] = {
                "latencyMs": latency_ms,
            }
        return {
            "event": {
                "metadata": metadata_info
            }
        }
    
    @staticmethod
    def error(error_message: str, code: Optional[str] = None) -> Dict[str, Any]:
        """Format error event in Strands Agent format"""
        error_info: Dict[str, Any] = {
            "error": error_message,
        }
        if code:
            error_info["code"] = code
        return {
            "event": {
                "error": error_info
            }
        }
    
    @staticmethod
    def agent_status(
        label: str,
        phase: Optional[str] = None,
        elapsed_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Format a transient progress event.

        Not message content: it says what the agent is doing while it produces
        nothing, and it is replaced by the answer rather than kept beside it. So
        it is deliberately its own event kind — routing it through
        contentBlockDelta would persist "analysing…" into the saved transcript.
        """
        status_info: Dict[str, Any] = {"label": label}
        if phase:
            status_info["phase"] = phase
        if elapsed_ms is not None:
            status_info["elapsedMs"] = elapsed_ms
        return {
            "event": {
                "agentStatus": status_info
            }
        }

    @staticmethod
    def chart(
        spec: Optional[Dict[str, Any]] = None,
        url: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Format a chart the runtime produced.

        `spec` ({kind, data, encoding, title}) is the durable form: it has no expiry,
        so the UI redraws it on every render, in the current theme. The image is
        carried too but is not a substitute — a runtime-rendered chart is handed over
        as a presigned URL good for five minutes while the object behind it lives for
        days, so a thread reopened later would show a broken image.

        An `s3://` URI is deliberately *not* forwarded. A browser cannot load one and
        this server has no presign route for the runtime's staging bucket, so it
        would only invite a consumer to try. The caller drops a frame that has
        neither a spec nor a loadable url; today that means a chart drawn in a code
        sandbox (arbitrary forms — boxplots, dual-axis) does not render, and the fix
        for that is a presign route rather than passing the URI through.
        """
        # `spec` is always present, null included: the UI branches on it to decide
        # between redrawing and falling back to the image, and an absent key would
        # be indistinguishable from a spec that failed to serialise.
        chart_info: Dict[str, Any] = {"spec": spec}
        if url:
            chart_info["url"] = url
        if source:
            chart_info["source"] = source
        return {
            "event": {
                "chart": chart_info
            }
        }

    @staticmethod
    def verification(result: Dict[str, Any]) -> Dict[str, Any]:
        """Format evidence for how a number was checked.

        `ask_sql_verified` generates k candidate queries, executes each, and adopts
        the result the majority agree on. The consensus meta (method, k, n_valid,
        agreement, verdict) is why the answer can be trusted, so it is surfaced
        rather than kept in the logs — advisory, never a gate.
        """
        return {
            "event": {
                "verification": result
            }
        }

    @staticmethod
    def end(status: str = "completed") -> Dict[str, Any]:
        """Format end event"""
        return {
            "event": {
                "end": {
                    "status": status
                }
            }
        }
    
    @staticmethod
    def web_search_results(results: list) -> Dict[str, Any]:
        """Format web_search_results event"""
        return {
            "event": {
                "webSearchResults": {
                    "results": results,
                    "status": "done",
                }
            }
        }
    
    @staticmethod
    def content_block_start(
        content_block_index: int = 0,
        block_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Format contentBlockStart event in Strands Agent format"""
        start_info: Dict[str, Any] = {}
        if block_type:
            start_info["type"] = block_type
        return {
            "event": {
                "contentBlockStart": {
                    "index": content_block_index,
                    "start": start_info,
                }
            }
        }
    
    @staticmethod
    def content_block_stop(content_block_index: int = 0) -> Dict[str, Any]:
        """Format contentBlockStop event in Strands Agent format"""
        return {
            "event": {
                "contentBlockStop": {
                    "index": content_block_index,
                }
            }
        }
    
    @staticmethod
    def tool_result(tool_use_id: str, result: Any) -> Dict[str, Any]:
        """Format toolResult event in Strands Agent format.

        No `status`: the typed runtime frames this serves carry no notion of a
        tool failing, and stamping "success" on them would assert something the
        stream never said. The harness path builds its own toolResult and does
        add one (see harness_event_adapter) because InvokeHarness reports the
        verdict — so consumers must treat the key as optional.

        Consumers assign this result rather than appending to it, which is what
        lets the harness path stream a long result as many events each carrying
        the running total.
        """
        return {
            "event": {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "result": result,
                }
            }
        }

