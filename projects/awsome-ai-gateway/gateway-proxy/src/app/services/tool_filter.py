# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Filter unsupported tool types before sending to Bedrock/Mantle.

Bedrock and Mantle do not support Anthropic's native built-in tool types
(web_search_20250305, code_execution_20250522, etc.) — they exist only on
Anthropic's direct API. When clients include these in the request body,
the backend returns 400: "tool type '...' is not supported for this model"

Web search is handled via the server-side loop (web_search_loop.py) or
AgentCore MCP; the native tool definitions are safely stripped here.

Claude Code also sends ``advisor_*`` (a tool Anthropic's API runs on the server):
with the advisor feature on it is in ``tools`` on EVERY request, so one unknown type
failed every request of that user, "hi" included. Removing a tool also removes what
points at it, or the request is still a 400:

- a ``tool_choice`` naming it becomes ``auto``;
- its history blocks (``server_tool_use`` naming it, ``<family>_tool_result``) — only for
  the server-tool families below. Replayed native web search blocks are left to the
  web-search path;
- ``tool_addition`` / ``tool_removal`` blocks (mid-conversation tool changes, Claude Code
  2.1.29x, beta ``inline-tools``) that name it, or that carry an inline definition of an
  unsupported type — Bedrock answers "references unknown tool 'advisor'".

This filter runs on the body builder AFTER allowed-fields filtering and
BEFORE sending to the adapter.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Tool type prefixes that Bedrock/Mantle do not support.
# These are Anthropic-native built-in tools, not standard function tools.
_UNSUPPORTED_TOOL_PREFIXES = (
    "web_search_",
    "code_execution_",
    "text_editor_",
    "computer_",
    "advisor_",
)

# Server-run tools whose history blocks are removed with the tool. Web search is not
# here: its replayed blocks are rewritten by the web-search path, not dropped.
_HISTORY_TOOL_PREFIXES = ("advisor_",)

_TOOL_CHANGE_BLOCKS = ("tool_addition", "tool_removal")

#: Stands in for a message that held nothing but removed blocks (empty content is a 400).
_REMOVED_NOTE = "[a server tool result that this endpoint cannot replay was removed]"
_REMOVED_TOOL_CHANGE_NOTE = "[a tool change that this endpoint cannot apply was removed]"


def _family(tool_type: str) -> str:
    """``advisor_20260301`` → ``advisor`` (its result blocks are ``advisor_tool_result``)."""
    head, _, tail = tool_type.rpartition("_")
    return head if head and tail.isdigit() else tool_type


def _tool_change_target(block: Any) -> tuple[str | None, str | None]:
    """``(name, type)`` of the tool a ``tool_addition`` / ``tool_removal`` block points
    at — a ``tool_reference`` carries only the name, a ``tool_definition`` the whole tool."""
    if not (isinstance(block, dict) and block.get("type") in _TOOL_CHANGE_BLOCKS):
        return None, None
    tool = block.get("tool")
    if not isinstance(tool, dict):
        return None, None
    if tool.get("type") == "tool_definition":
        d = tool.get("definition")
        if isinstance(d, dict):
            t = d.get("type")
            return d.get("name"), t if isinstance(t, str) else None
        return None, None
    return tool.get("name"), None


def _strip_messages(messages: list, names: set, history_names: set,
                    result_types: set) -> list | None:
    """New messages without the blocks of removed tools, or None when nothing changed.

    Copy-on-write: the body builder copies the request shallowly, so these message dicts
    are also the web-search loop's conversation and the body-log record.
    """

    def _drop(b: Any) -> bool:
        if not isinstance(b, dict):
            return False
        if b.get("type") == "server_tool_use" and b.get("name") in history_names:
            return True
        if b.get("type") in result_types:
            return True
        name, ttype = _tool_change_target(b)
        return bool((name and name in names)
                    or (ttype and ttype.startswith(_UNSUPPORTED_TOOL_PREFIXES)))

    out: list = []
    changed = False
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            out.append(m)
            continue
        keep = [b for b in content if not _drop(b)]
        if len(keep) == len(content):
            out.append(m)
            continue
        changed = True
        cache = next((b["cache_control"] for b in content
                      if _drop(b) and isinstance(b.get("cache_control"), dict)), None)
        if not keep:
            note = _REMOVED_TOOL_CHANGE_NOTE if m.get("role") == "system" else _REMOVED_NOTE
            keep = [{"type": "text", "text": note}]
        last = keep[-1]
        # Keep the cache breakpoint where it was (thinking blocks cannot carry one).
        if (cache is not None and isinstance(last, dict) and "cache_control" not in last
                and last.get("type") not in ("thinking", "redacted_thinking")):
            keep[-1] = {**last, "cache_control": cache}
        out.append({**m, "content": keep})
    return out if changed else None


def strip_unsupported_tools(body: dict[str, Any], *, request_id: str = "") -> dict[str, Any]:
    """Remove tool definitions that Bedrock/Mantle cannot process.

    Standard function tools (type="function" or type="custom" or no type) are
    kept. Only Anthropic-native built-in tool types (web_search_*, etc.) are
    stripped. If all tools are stripped, the tools field itself is removed.
    Blocks that point at a stripped tool go too (module docstring).

    Never raises — unsupported shapes pass through unchanged.
    """
    try:
        tools = body.get("tools")
        kept = []
        stripped = []
        for tool in tools if isinstance(tools, list) else []:
            if not isinstance(tool, dict):
                kept.append(tool)
                continue
            t_type = tool.get("type", "")
            if isinstance(t_type, str) and t_type.startswith(_UNSUPPORTED_TOOL_PREFIXES):
                stripped.append(tool)
            else:
                kept.append(tool)

        messages = body.get("messages")
        # Inline definitions in system messages count as stripped tools even when the
        # tool is not in `tools` at all.
        inline = []
        if isinstance(messages, list):
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "system" \
                        and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        name, ttype = _tool_change_target(b)
                        if ttype and ttype.startswith(_UNSUPPORTED_TOOL_PREFIXES):
                            inline.append({"type": ttype, "name": name})
        if not stripped and not inline:
            return body

        if stripped:
            stripped_names = {t.get("name") for t in stripped if t.get("name")}
            logger.info(
                "tools_stripped",
                request_id=request_id,
                stripped_types=[t.get("type") for t in stripped],
                kept_count=len(kept),
            )
            if kept:
                body["tools"] = kept
                tc = body.get("tool_choice")
                if isinstance(tc, dict) and tc.get("type") == "tool" \
                        and tc.get("name") in stripped_names:
                    body["tool_choice"] = {"type": "auto"}
            else:
                body.pop("tools", None)
                body.pop("tool_choice", None)

        if isinstance(messages, list):
            gone = stripped + inline
            names = {t.get("name") for t in gone if t.get("name")}
            result_types = {f"{_family(t['type'])}_tool_result" for t in gone
                            if isinstance(t.get("type"), str)
                            and t["type"].startswith(_HISTORY_TOOL_PREFIXES)}
            history_names = {t.get("name") for t in gone
                             if isinstance(t.get("type"), str)
                             and t["type"].startswith(_HISTORY_TOOL_PREFIXES)}
            new_messages = _strip_messages(messages, names, history_names, result_types) \
                if names or result_types else None
            if new_messages is not None:
                body["messages"] = new_messages
                logger.info("tool_blocks_stripped", request_id=request_id,
                            tools=sorted(n for n in names if n),
                            history=sorted(n for n in history_names if n))
        return body
    except Exception:
        logger.warning("tool_filter_failed", request_id=request_id, exc_info=True)
        return body
