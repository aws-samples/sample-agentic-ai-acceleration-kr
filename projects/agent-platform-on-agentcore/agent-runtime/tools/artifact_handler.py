"""Artifact tool handler.

Turns a completed `create_artifact` / `update_artifact` tool call into an
`artifact` stream event. The platform server stores the content in S3 and
enriches the event with the artifact id and version before it reaches the UI.
"""
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ARTIFACT_KINDS = {"markdown", "code", "html", "svg", "mermaid", "csv", "json", "text"}


class ArtifactHandler:
    """Handler for artifact tools that emits `artifact` stream events."""

    ARTIFACT_TOOL_NAMES = ["create_artifact", "update_artifact"]

    @classmethod
    def is_artifact_tool(cls, tool_name: str) -> bool:
        return tool_name in cls.ARTIFACT_TOOL_NAMES

    @classmethod
    def process_artifact_tool_call(
        cls,
        tool_name: str,
        tool_input: Dict[str, Any],
        tool_call_id: str,
        message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not cls.is_artifact_tool(tool_name):
            return None

        content = tool_input.get("content")
        if not content:
            logger.warning("Artifact tool %s called without content", tool_name)
            return None

        kind = str(tool_input.get("kind", "")).strip().lower()
        if kind not in ARTIFACT_KINDS:
            kind = "text"

        artifact: Dict[str, Any] = {
            "toolCallId": tool_call_id,
            "artifactId": tool_input.get("artifact_id") or None,
            "title": tool_input.get("title") or "Untitled",
            "kind": kind,
            "content": content,
        }
        if message_id:
            artifact["messageId"] = message_id
        language = tool_input.get("language")
        if language:
            artifact["language"] = str(language).strip().lower()

        event = {"event": {"artifact": artifact}}
        try:
            json.dumps(event)
        except (TypeError, ValueError) as exc:
            logger.error("Artifact event not JSON serializable: %s", exc)
            return None

        logger.info("Emitting artifact '%s' (%s, %d chars)", artifact["title"], kind, len(content))
        return event
