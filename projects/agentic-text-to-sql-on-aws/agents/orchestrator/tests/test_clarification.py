"""clarification 순수 로직 테스트 (reason 빌더, fields 정규화, value 변환, resume task).

create_clarification_tool 은 strands 를 지연 임포트하므로 여기서는 다루지 않는다.
"""

from dataclasses import dataclass
from typing import Any

from orchestrator.clarification import (
    CLARIFICATION_INTERRUPT_NAME,
    build_reason,
    build_resume_task,
    clarification_value,
    normalize_fields,
)


def test_normalize_fields_valid_select_keeps_options():
    fields = [
        {"name": "period", "label": "기간", "type": "select", "options": ["오늘", "이번주"]},
    ]
    out = normalize_fields(fields)
    assert out == [
        {"name": "period", "label": "기간", "type": "select", "options": ["오늘", "이번주"]}
    ]


def test_normalize_fields_label_defaults_to_name():
    out = normalize_fields([{"name": "region", "type": "text"}])
    assert out == [{"name": "region", "label": "region", "type": "text"}]


def test_normalize_fields_unknown_type_downgraded_to_text():
    out = normalize_fields([{"name": "x", "label": "X", "type": "slider"}])
    assert out[0]["type"] == "text"
    # text 타입은 options 를 갖지 않음
    assert "options" not in out[0]


def test_normalize_fields_drops_entries_without_name():
    out = normalize_fields([{"label": "no name"}, {"name": "  ", "type": "text"}, 123, "str"])
    assert out == []


def test_normalize_fields_select_without_options_gets_empty_list():
    out = normalize_fields([{"name": "p", "type": "select"}])
    assert out[0]["options"] == []


def test_normalize_fields_select_options_coerced_to_str():
    out = normalize_fields([{"name": "p", "type": "select", "options": [1, 2, None]}])
    assert out[0]["options"] == ["1", "2", "None"]


def test_normalize_fields_non_list_returns_empty():
    assert normalize_fields(None) == []
    assert normalize_fields("oops") == []


def test_build_reason_shapes_payload():
    reason = build_reason("기간은?", [{"name": "p", "type": "text"}])
    assert reason["question"] == "기간은?"
    assert reason["fields"] == [{"name": "p", "label": "p", "type": "text"}]


def test_build_reason_none_question():
    reason = build_reason(None, None)
    assert reason["question"] == ""
    assert reason["fields"] == []


@dataclass
class FakeInterrupt:
    id: str
    name: str
    reason: Any = None


def test_clarification_value_from_object():
    itr = FakeInterrupt(
        id="i-1",
        name=CLARIFICATION_INTERRUPT_NAME,
        reason={
            "question": "기간은?",
            "fields": [{"name": "p", "type": "select", "options": ["a"]}],
        },
    )
    val = clarification_value(itr)
    assert val == {
        "interruptId": "i-1",
        "interruptName": "clarification",
        "question": "기간은?",
        "fields": [{"name": "p", "label": "p", "type": "select", "options": ["a"]}],
    }


def test_clarification_value_from_dict():
    itr = {"id": "i-2", "name": "clarification", "reason": {"question": "무엇을?", "fields": []}}
    val = clarification_value(itr)
    assert val["interruptId"] == "i-2"
    assert val["question"] == "무엇을?"
    assert val["fields"] == []


def test_clarification_value_defensive_non_dict_reason():
    # reason 이 기대 형태가 아니면 question=str(reason), fields=[]
    itr = FakeInterrupt(id="i-3", name="clarification", reason="그냥 문자열 사유")
    val = clarification_value(itr)
    assert val["question"] == "그냥 문자열 사유"
    assert val["fields"] == []


def test_clarification_value_missing_name_defaults():
    itr = {"id": "i-4", "reason": {"question": "q", "fields": []}}
    val = clarification_value(itr)
    assert val["interruptName"] == "clarification"


def test_clarification_value_missing_id_empty_string():
    itr = {"name": "clarification", "reason": {"question": "q", "fields": []}}
    val = clarification_value(itr)
    assert val["interruptId"] == ""


def test_build_resume_task_shape():
    task = build_resume_task({"interruptId": "i-9", "values": {"period": "이번달"}})
    assert task == [
        {"interruptResponse": {"interruptId": "i-9", "response": {"period": "이번달"}}}
    ]


def test_create_clarification_tool_with_real_sdk():
    """실 strands SDK 로 도구 생성이 성공하는지 검증.

    `from __future__ import annotations` + 지연 임포트 조합에서 데코레이터의
    get_type_hints 평가가 NameError 를 내는 회귀(배포에서 발견)를 방지한다.
    """
    pytest = __import__("pytest")
    pytest.importorskip("strands")
    from orchestrator.clarification import create_clarification_tool

    tool = create_clarification_tool()
    assert getattr(tool, "tool_name", None) == "request_clarification"
