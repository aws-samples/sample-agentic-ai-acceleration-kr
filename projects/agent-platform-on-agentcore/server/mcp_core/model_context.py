"""MCP Apps `ui/update-model-context` 를 다음 턴의 모델 입력에 싣는다.

규격(SEP-1865): 앱이 `ui/update-model-context` 로 보낸 내용을 호스트는 "이후 턴에서
모델에게 제공해야 한다(SHOULD)". 다음 사용자 메시지 전에 여러 번 오면 마지막 것만
보낸다 — 그 선별은 브라우저가 하고(`useChat`), 서버는 받은 하나를 싣기만 한다.

**저장본과 모델 입력을 분리한다.** 컨텍스트는 사용자가 쓴 말이 아니므로 스레드에
저장되는 human 메시지에는 넣지 않는다. 런타임/harness 로 나가는 사본의 마지막 human
메시지에만 덧붙인다. 그래서 이 함수는 새 리스트를 돌려주고 입력을 건드리지 않는다.

두 클라이언트가 모두 읽을 수 있는 형태여야 한다: 런타임(`AgentCoreClient._split_content`)
과 harness(`MessageUtils.build_strands_conversation`)는 content 가 문자열이면 그대로,
리스트면 `text` 블록들을 이어 읽는다. 그래서 문자열에는 이어 붙이고, 리스트에는
`{"type": "text", "text": ...}` 블록을 하나 더 붙인다.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional

APP_CONTEXT_HEADER = "[MCP App context]"
APP_CONTEXT_NOTE = (
    "대화 안의 MCP App 이 ui/update-model-context 로 제공한 보조 컨텍스트입니다. "
    "사용자가 쓴 말이 아니며, 답변에 참고만 하십시오."
)


def render_app_model_context(context: Any) -> Optional[str]:
    """`ui/update-model-context` params → 모델이 읽을 텍스트. 내용이 없으면 None."""
    if not isinstance(context, dict):
        return None

    parts: List[str] = []

    content = context.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())

    structured = context.get("structuredContent")
    if isinstance(structured, dict) and structured:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))

    if not parts:
        return None
    return "\n".join([APP_CONTEXT_HEADER, APP_CONTEXT_NOTE, *parts])


def inject_app_model_context(
    messages: List[Dict[str, Any]], context: Any
) -> List[Dict[str, Any]]:
    """마지막 human 메시지에 컨텍스트를 덧붙인 **새** 메시지 리스트를 돌려준다.

    붙일 내용이 없거나 human 메시지가 없으면 입력을 그대로 돌려준다(같은 객체).
    """
    rendered = render_app_model_context(context)
    if not rendered:
        return messages

    index = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("type") in ("human", "user"):
            index = i
            break
    if index is None:
        return messages

    target = copy.deepcopy(messages[index])
    content = target.get("content", "")
    if isinstance(content, list):
        # 런타임의 `_split_content` 는 text 블록을 구분자 없이 이어 붙이므로 앞에 빈
        # 줄을 넣어 사용자 문장과 붙지 않게 한다.
        target["content"] = [*content, {"type": "text", "text": f"\n\n{rendered}"}]
    else:
        base = content if isinstance(content, str) else str(content)
        target["content"] = f"{base}\n\n{rendered}" if base else rendered

    return [*messages[:index], target, *messages[index + 1 :]]
