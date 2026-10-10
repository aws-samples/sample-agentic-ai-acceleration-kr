# Copyright 2026 © Amazon.com and Affiliates.
"""Tests for strip_unsupported_tools."""

import copy

from app.services.tool_filter import strip_unsupported_tools


def test_web_search_tool_stripped():
    body = {
        "model": "anthropic.claude-sonnet-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    result = strip_unsupported_tools(body, request_id="test-1")
    assert "tools" not in result


def test_function_tools_kept():
    body = {
        "model": "anthropic.claude-opus-4-8",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "name": "get_weather", "input_schema": {}},
            {"type": "custom", "name": "my_tool"},
        ],
    }
    result = strip_unsupported_tools(body, request_id="test-2")
    assert len(result["tools"]) == 2


def test_mixed_tools_only_unsupported_stripped():
    body = {
        "model": "anthropic.claude-opus-4-8",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "web_search_20250305", "name": "web_search"},
            {"type": "function", "name": "get_weather", "input_schema": {}},
        ],
        "tool_choice": {"type": "auto"},
    }
    result = strip_unsupported_tools(body, request_id="test-3")
    assert len(result["tools"]) == 1
    assert result["tools"][0]["name"] == "get_weather"
    assert "tool_choice" in result


def test_all_tools_stripped_removes_tool_choice():
    body = {
        "model": "anthropic.claude-opus-4-8",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "tool_choice": {"type": "auto"},
    }
    result = strip_unsupported_tools(body, request_id="test-4")
    assert "tools" not in result
    assert "tool_choice" not in result


def test_code_execution_tool_stripped():
    body = {
        "tools": [{"type": "code_execution_20250522", "name": "code_exec"}],
    }
    result = strip_unsupported_tools(body, request_id="test-5")
    assert "tools" not in result


def test_computer_tool_stripped():
    body = {
        "tools": [{"type": "computer_20250124", "name": "computer"}],
    }
    result = strip_unsupported_tools(body, request_id="test-6")
    assert "tools" not in result


def test_text_editor_tool_stripped():
    body = {
        "tools": [{"type": "text_editor_20250124", "name": "text_editor"}],
    }
    result = strip_unsupported_tools(body, request_id="test-7")
    assert "tools" not in result


def test_no_tools_field_is_noop():
    body = {"model": "anthropic.claude-opus-4-8", "messages": []}
    result = strip_unsupported_tools(body, request_id="test-8")
    assert "tools" not in result


def test_empty_tools_is_noop():
    body = {"tools": []}
    result = strip_unsupported_tools(body, request_id="test-9")
    assert result["tools"] == []


def test_malformed_tool_does_not_raise():
    body = {"tools": [None, 123, "bad"]}
    result = strip_unsupported_tools(body, request_id="test-10")
    assert len(result["tools"]) == 3


# ── Claude Code: Anthropic-only server tools and mid-conversation tool changes ──
# A Claude Code session with the advisor feature on lists {"type": "advisor_20260301"}
# in `tools` on every request and, from 2.1.29x, points at it from a system message
# with a `tool_addition` block. Bedrock rejects the tool type, and a tool_addition that
# names a tool missing from `tools` ("references unknown tool 'advisor'").

ADVISOR = {"type": "advisor_20260301", "name": "advisor", "model": "claude-fable-5-1"}
READ = {"name": "Read", "description": "read a file", "input_schema": {"type": "object"}}
CC = {"type": "ephemeral"}


def _ref(name, kind="tool_addition", cache=False):
    block = {"type": kind, "tool": {"type": "tool_reference", "name": name}}
    if cache:
        block["cache_control"] = dict(CC)
    return block


def _text(text="reminder", cache=False):
    block = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = dict(CC)
    return block


def _with_system(tools, *blocks, **keys):
    return {"tools": tools,
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "system", "content": list(blocks), **keys}]}


def test_advisor_tool_stripped():
    result = strip_unsupported_tools({"tools": [READ, ADVISOR]}, request_id="t-adv")
    assert result["tools"] == [READ]


def test_tool_choice_naming_a_stripped_tool_becomes_auto():
    body = {"tools": [READ, ADVISOR], "tool_choice": {"type": "tool", "name": "advisor"}}
    result = strip_unsupported_tools(body, request_id="t-tc")
    assert result["tool_choice"] == {"type": "auto"}


def test_tool_choice_naming_a_kept_tool_is_untouched():
    body = {"tools": [READ, ADVISOR], "tool_choice": {"type": "tool", "name": "Read"}}
    result = strip_unsupported_tools(body, request_id="t-tc2")
    assert result["tool_choice"] == {"type": "tool", "name": "Read"}


def test_advisor_history_blocks_removed_never_leaving_an_empty_message():
    body = {"tools": [READ, ADVISOR], "messages": [
        {"role": "user", "content": "plan this"},
        {"role": "assistant", "content": [
            _text("let me check"),
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "advisor", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_1", "content": {}}]},
        {"role": "user", "content": "go on"},
        {"role": "assistant", "content": [
            {"type": "server_tool_use", "id": "srvtoolu_2", "name": "advisor", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srvtoolu_2", "content": {}}]}]}
    result = strip_unsupported_tools(body, request_id="t-hist")
    assert [b["type"] for b in result["messages"][1]["content"]] == ["text"]
    [note] = result["messages"][3]["content"]
    assert note["type"] == "text" and note["text"].strip()


def test_web_search_history_blocks_are_left_to_the_web_search_path():
    replay = [{"type": "server_tool_use", "id": "srvtoolu_9", "name": "web_search", "input": {}},
              {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_9", "content": []}]
    body = {"tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": "q"},
                         {"role": "assistant", "content": copy.deepcopy(replay)}]}
    result = strip_unsupported_tools(body, request_id="t-ws")
    assert result["messages"][1]["content"] == replay


def test_tool_addition_naming_a_stripped_tool_goes_and_its_cache_mark_moves():
    body = _with_system([READ, ADVISOR], _text(), _ref("advisor", cache=True),
                        output_config={"effort": "medium"})
    result = strip_unsupported_tools(body, request_id="t-ta")
    system = result["messages"][1]
    assert system["content"] == [_text(cache=True)]
    assert system["output_config"] == {"effort": "medium"}


def test_cache_mark_is_not_doubled():
    body = _with_system([READ, ADVISOR], _text(cache=True), _ref("advisor", cache=True))
    result = strip_unsupported_tools(body, request_id="t-cc")
    assert result["messages"][1]["content"] == [_text(cache=True)]


def test_tool_addition_naming_a_kept_tool_stays():
    body = _with_system([READ, ADVISOR], _text(), _ref("Read"), _ref("advisor"))
    result = strip_unsupported_tools(body, request_id="t-keep")
    assert result["messages"][1]["content"] == [_text(), _ref("Read")]


def test_tool_removal_naming_a_stripped_tool_goes():
    body = _with_system([READ, ADVISOR], _text(), _ref("advisor", kind="tool_removal"))
    result = strip_unsupported_tools(body, request_id="t-rm")
    assert result["messages"][1]["content"] == [_text()]


def test_inline_definition_of_an_unsupported_type_goes_without_it_in_tools():
    inline = {"type": "tool_addition",
              "tool": {"type": "tool_definition", "definition": dict(ADVISOR)}}
    body = _with_system([READ], _text(), inline)
    body["messages"].append({"role": "system", "content": [
        _text("later"), _ref("advisor", kind="tool_removal")]})
    result = strip_unsupported_tools(body, request_id="t-inline")
    assert result["tools"] == [READ]
    assert result["messages"][1]["content"] == [_text()]
    assert result["messages"][2]["content"] == [_text("later")]


def test_system_message_left_empty_gets_a_note_and_keeps_its_other_keys():
    body = _with_system([READ, ADVISOR], _ref("advisor", cache=True),
                        output_config={"effort": "high"})
    result = strip_unsupported_tools(body, request_id="t-empty")
    system = result["messages"][1]
    [note] = system["content"]
    assert note["type"] == "text" and note["text"].strip()
    assert note["cache_control"] == CC
    assert system["output_config"] == {"effort": "high"}


def test_caller_messages_are_not_mutated():
    # The body builder copies the request shallowly; the same message dicts are also the
    # web-search loop's conversation and the body-log record.
    messages = [{"role": "user", "content": "hi"},
                {"role": "system", "content": [_text(), _ref("advisor", cache=True)]}]
    before = copy.deepcopy(messages)
    body = {"tools": [READ, ADVISOR], "messages": messages}
    strip_unsupported_tools(body, request_id="t-cow")
    assert messages == before


def test_nothing_to_strip_leaves_messages_object_alone():
    body = _with_system([READ], _text(), _ref("Read"))
    messages = body["messages"]
    result = strip_unsupported_tools(body, request_id="t-same")
    assert result["messages"] is messages


def test_malformed_messages_do_not_raise():
    body = {"tools": [READ, ADVISOR],
            "messages": [None, "x", {"role": "system",
                                     "content": [None, 5, {"type": "tool_addition"}]}]}
    result = strip_unsupported_tools(body, request_id="t-bad")
    assert result["tools"] == [READ]


# ── Mantle bodies (Cowork / Codex) go through the same filter ──────────────────
def test_mantle_shaped_body_without_tool_changes_is_unchanged():
    body = {"model": "anthropic.claude-opus-4-8", "stream": True,
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": [_text("ok")]}],
            "tools": [{"type": "custom", "name": "my_tool"}]}
    before = copy.deepcopy(body)
    messages = body["messages"]
    result = strip_unsupported_tools(body, request_id="t-mantle")
    assert result == before
    assert result["messages"] is messages


def test_tool_addition_naming_a_stripped_native_tool_goes():
    body = {"model": "anthropic.claude-opus-4-8",
            "tools": [{"type": "web_search_20250305", "name": "web_search"}, READ],
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "system", "content": [_text(), _ref("web_search")]}]}
    result = strip_unsupported_tools(body, request_id="t-mantle-ta")
    assert result["tools"] == [READ]
    assert result["messages"][1]["content"] == [_text()]
