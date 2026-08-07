# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for strip_unsupported_tools."""

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
