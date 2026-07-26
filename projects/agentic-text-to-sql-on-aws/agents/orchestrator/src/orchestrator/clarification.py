"""clarification(재요청) 도구 및 interrupt payload 순수 로직.

intent 노드(및 단일 Agent 폴백)에 부착하는 로컬 Python 도구를 정의한다(MCP 아님).
질의가 모호할 때 LLM 이 `request_clarification` 을 호출하면 Strands interrupt 가 발생해
agent 실행이 정지하고, 사용자가 폼 응답을 보내면 그 값이 도구 반환값으로 재개된다.

순수 로직(reason payload 빌더, fields 정규화, Interrupt → AG-UI value 변환)과 SDK 결합
(도구 팩토리, strands 지연 임포트)을 분리해 단위 테스트가 SDK 없이 돌아가게 한다.

## AG-UI 표면화 계약 (변경 금지)
- interrupt 발생 시 stream_translator 가 다음 CUSTOM 이벤트를 방출:
    {"type":"CUSTOM","name":"clarification_request",
     "value":{"interruptId":str,"interruptName":"clarification","question":str,"fields":[...]}}
- 재개는 새 HTTP 요청으로 forwardedProps.clarificationResponse 를 통해 들어온다(request.py).
"""

from __future__ import annotations

from typing import Any

# interrupt 이름(고정). 재개 시 이름 매칭 및 UI 표면화에 사용.
CLARIFICATION_INTERRUPT_NAME = "clarification"

# 폼 필드 타입 화이트리스트. 이외 값은 방어적으로 "text" 로 강등한다.
ALLOWED_FIELD_TYPES = ("select", "date_range", "text")


def normalize_fields(fields: Any) -> list[dict[str, Any]]:
    """clarification 폼 필드 목록을 검증·정규화한다.

    각 원소 형태: {"name": str, "label": str, "type": "select"|"date_range"|"text",
                  "options": [str] (select 만)}.
    - name 이 없거나 문자열이 아니면 그 원소는 버린다.
    - label 이 없으면 name 으로 대체한다.
    - type 이 화이트리스트 밖이면 "text" 로 강등한다.
    - options 는 select 타입에서만 유지하며 문자열 리스트로 강제한다.
    """
    if not isinstance(fields, list):
        return []
    normalized: list[dict[str, Any]] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()

        raw_type = field.get("type")
        field_type = raw_type.strip().lower() if isinstance(raw_type, str) else ""
        if field_type not in ALLOWED_FIELD_TYPES:
            field_type = "text"

        raw_label = field.get("label")
        label = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() else name

        entry: dict[str, Any] = {"name": name, "label": label, "type": field_type}
        if field_type == "select":
            options = field.get("options")
            entry["options"] = [str(o) for o in options] if isinstance(options, list) else []
        normalized.append(entry)
    return normalized


def build_reason(question: str, fields: Any) -> dict[str, Any]:
    """interrupt.reason payload 를 조립한다(JSON 직렬화 가능 dict)."""
    return {
        "question": str(question) if question is not None else "",
        "fields": normalize_fields(fields),
    }


def clarification_value(interrupt: Any) -> dict[str, Any]:
    """Strands Interrupt(객체 또는 dict)를 AG-UI CUSTOM value 로 변환(방어적).

    reason 이 기대한 {question, fields} 형태가 아니면 question=str(reason), fields=[] 로 방어한다.
    """
    interrupt_id = _get(interrupt, "id")
    interrupt_name = _get(interrupt, "name") or CLARIFICATION_INTERRUPT_NAME
    reason = _get(interrupt, "reason")

    if isinstance(reason, dict):
        raw_question = reason.get("question")
        question = str(raw_question) if raw_question is not None else ""
        fields = normalize_fields(reason.get("fields"))
    else:
        question = "" if reason is None else str(reason)
        fields = []

    return {
        "interruptId": str(interrupt_id) if interrupt_id is not None else "",
        "interruptName": str(interrupt_name),
        "question": question,
        "fields": fields,
    }


def build_resume_task(clarification_response: dict[str, Any]) -> list[dict[str, Any]]:
    """clarification 재개용 Strands task(interruptResponse 리스트)를 조립.

    clarification_response 형태: {"interruptId": str, "values": {...}}.
    반환: [{"interruptResponse": {"interruptId": <id>, "response": <values>}}].
    graph.stream_async / agent.stream_async 에 그대로 전달해 중단 지점부터 재개한다.
    """
    return [
        {
            "interruptResponse": {
                "interruptId": clarification_response.get("interruptId"),
                "response": clarification_response.get("values"),
            }
        }
    ]


def _get(obj: Any, key: str) -> Any:
    """dict 는 .get, 그 외는 getattr 로 속성을 방어적으로 추출."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def create_clarification_tool() -> Any:
    """`request_clarification` Strands 도구를 생성(SDK 지연 임포트).

    LLM 에 노출되는 시그니처:
        request_clarification(question: str, fields: list[dict]) -> dict
    반환: 사용자가 폼에 입력한 값 {"<field name>": <값>, ...}.

    구현: `tool_context.interrupt("clarification", reason=build_reason(...))` 의 결과를
    그대로 반환한다. 최초 호출 시 InterruptException 이 발생해 실행이 정지하고,
    사용자 응답으로 재개되면 그 응답(dict)이 interrupt 반환값으로 돌아온다.
    """
    from strands import tool
    from strands.types.tools import ToolContext

    # ⚠️ `from __future__ import annotations` 때문에 어노테이션이 문자열로 지연 평가되는데,
    # strands 데코레이터가 get_type_hints 로 이 모듈 전역에서 평가한다. 함수 내부 임포트만으로는
    # 전역에 ToolContext 가 없어 NameError 가 나므로 모듈 전역에 주입한다(지연 임포트 유지).
    globals()["ToolContext"] = ToolContext

    @tool(context=True)
    def request_clarification(
        question: str, fields: list[dict], tool_context: ToolContext
    ) -> dict:
        """질의가 모호할 때 사용자에게 되물어 필요한 정보를 받는다.

        Args:
            question: 사용자에게 보여줄 질문(예: "어느 기간의 매출을 말씀하시나요?").
            fields: 입력받을 폼 필드 목록. 각 원소는
                {"name": str, "label": str, "type": "select"|"date_range"|"text",
                 "options": [str] (select 만)} 형태.

        Returns:
            사용자가 폼에 입력한 값 {"<field name>": <값>, ...}.
        """
        reason = build_reason(question, fields)
        return tool_context.interrupt(CLARIFICATION_INTERRUPT_NAME, reason=reason)

    return request_clarification
