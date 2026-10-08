"""`ui/update-model-context` 가 다음 턴의 모델 입력에만 실린다.

규격 SHOULD: 호스트는 앱이 준 컨텍스트를 이후 턴에서 모델에게 제공한다. 저장되는
대화(human 메시지)는 사용자가 쓴 그대로여야 하므로, 런타임으로 나가는 사본에만 붙인다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_core.model_context import (  # noqa: E402
    APP_CONTEXT_HEADER,
    inject_app_model_context,
    render_app_model_context,
)

CTX = {"structuredContent": {"platformStatus": {"서버": "platform-status", "가동시간": "3분"}}}


def test_structured_content_is_rendered_as_json():
    text = render_app_model_context(CTX)
    assert text.startswith(APP_CONTEXT_HEADER)
    assert '"가동시간": "3분"' in text  # ensure_ascii=False — 모델이 그대로 읽는다


def test_text_blocks_are_rendered_too():
    text = render_app_model_context(
        {"content": [{"type": "text", "text": "선택된 행: 3"}, {"type": "image", "data": "…"}]}
    )
    assert "선택된 행: 3" in text
    assert "image" not in text


def test_empty_context_renders_nothing():
    assert render_app_model_context({}) is None
    assert render_app_model_context({"structuredContent": {}}) is None
    assert render_app_model_context("not a dict") is None


def test_string_content_gets_the_context_appended():
    messages = [
        {"id": "1", "type": "human", "content": "안녕"},
        {"id": "2", "type": "ai", "content": "네"},
        {"id": "3", "type": "human", "content": "상태 어때?"},
    ]
    out = inject_app_model_context(messages, CTX)

    assert out[2]["content"].startswith("상태 어때?\n\n" + APP_CONTEXT_HEADER)
    # 이전 human 메시지는 그대로, ai 메시지도 그대로.
    assert out[0] == messages[0]
    assert out[1] == messages[1]


def test_block_content_gets_a_text_block_appended():
    """첨부가 있는 메시지는 content 가 블록 배열이다. 첨부 참조를 건드리지 않고 text 블록을
    하나 더 붙인다 — 런타임의 `_split_content` 와 harness 의 `build_strands_conversation`
    둘 다 text 블록을 이어 읽는다."""
    messages = [
        {
            "id": "1",
            "type": "human",
            "content": [{"type": "text", "text": "이 파일 봐"}, {"type": "attachment", "id": "a-1"}],
        }
    ]
    out = inject_app_model_context(messages, CTX)

    assert out[0]["content"][0] == {"type": "text", "text": "이 파일 봐"}
    assert out[0]["content"][1] == {"type": "attachment", "id": "a-1"}
    assert out[0]["content"][2]["type"] == "text"
    assert APP_CONTEXT_HEADER in out[0]["content"][2]["text"]


def test_the_input_is_not_mutated():
    """저장본은 사용자가 쓴 그대로여야 한다."""
    messages = [{"id": "1", "type": "human", "content": "상태 어때?"}]
    inject_app_model_context(messages, CTX)

    assert messages[0]["content"] == "상태 어때?"


def test_no_context_returns_the_same_list():
    messages = [{"id": "1", "type": "human", "content": "안녕"}]
    assert inject_app_model_context(messages, None) is messages
    assert inject_app_model_context(messages, {}) is messages


def test_no_human_message_returns_the_same_list():
    messages = [{"id": "1", "type": "ai", "content": "네"}]
    assert inject_app_model_context(messages, CTX) is messages
