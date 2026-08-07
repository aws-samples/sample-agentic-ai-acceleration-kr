# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Filter unsupported tool types before sending to Bedrock/Mantle.

Bedrock and Mantle do not support Anthropic's native built-in tool types
(web_search_20250305, code_execution_20250522, etc.) — they exist only on
Anthropic's direct API. When clients include these in the request body,
the backend returns 400: "tool type '...' is not supported for this model"

Web search is handled via the server-side loop (web_search_loop.py) or
AgentCore MCP; the native tool definitions are safely stripped here.

This filter runs on the body builder AFTER allowed-fields filtering and
BEFORE sending to the adapter.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Tool type prefixes that Bedrock/Mantle do not support.
_UNSUPPORTED_TOOL_PREFIXES = (
    "web_search_",
    "code_execution_",
    "text_editor_",
    "computer_",
)


def strip_unsupported_tools(body: dict[str, Any], *, request_id: str = "") -> dict[str, Any]:
    """Remove tool definitions that Bedrock/Mantle cannot process.

    Standard function tools (type="function" or type="custom" or no type) are
    kept. Only Anthropic-native built-in tool types (web_search_*, etc.) are
    stripped. If all tools are stripped, the tools field itself is removed.

    Never raises — unsupported shapes pass through unchanged.
    """
    try:
        tools = body.get("tools")
        if not isinstance(tools, list) or not tools:
            return body

        kept = []
        stripped = []
        for tool in tools:
            if not isinstance(tool, dict):
                kept.append(tool)
                continue
            t_type = tool.get("type", "")
            if isinstance(t_type, str) and t_type.startswith(_UNSUPPORTED_TOOL_PREFIXES):
                stripped.append(t_type)
            else:
                kept.append(tool)

        if stripped:
            logger.info(
                "tools_stripped",
                request_id=request_id,
                stripped_types=stripped,
                kept_count=len(kept),
            )
            if kept:
                body["tools"] = kept
            else:
                body.pop("tools", None)
                body.pop("tool_choice", None)

        return body
    except Exception:
        logger.warning("tool_filter_failed", request_id=request_id, exc_info=True)
        return body
