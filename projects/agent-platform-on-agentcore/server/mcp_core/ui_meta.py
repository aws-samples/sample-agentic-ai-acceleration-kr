"""MCP Apps(_meta.ui) 해석 규칙.

규격: SEP-1865 (io.modelcontextprotocol/ui, Stable 2026-01-26).

여기 있는 함수는 전부 순수 함수다. `_meta`는 신뢰할 수 없는 MCP 서버가 보낸 값이므로
형태가 어긋나도 예외를 던지지 않고 안전한 기본값으로 떨어진다.
"""
from typing import Any, Dict, List, Optional

UI_EXTENSION_ID = "io.modelcontextprotocol/ui"
APP_MIME_TYPE = "text/html;profile=mcp-app"
UI_URI_SCHEME = "ui://"

_DEFAULT_VISIBILITY = ["model", "app"]


def ui_client_capabilities() -> Dict[str, Any]:
    """initialize 요청에 넣을 확장 capability.

    `mimeTypes`는 규격상 REQUIRED다.
    """
    return {"extensions": {UI_EXTENSION_ID: {"mimeTypes": [APP_MIME_TYPE]}}}


def _ui_block(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    block = meta.get("ui")
    return block if isinstance(block, dict) else {}


def resource_uri_of(meta: Optional[Dict[str, Any]]) -> Optional[str]:
    """툴에 연결된 ui:// 리소스 URI. 없거나 ui:// 스킴이 아니면 None.

    스킴을 검사하는 이유: 호스트가 이 값을 그대로 가져오므로, https:// 를 허용하면
    악성 서버가 임의 URL을 호스트에게 읽히게 할 수 있다.
    """
    uri = _ui_block(meta).get("resourceUri")
    if not isinstance(uri, str):
        # 평면형 키는 규격이 deprecated로 표시했으나 GA 전까지 들어올 수 있다.
        uri = meta.get("ui/resourceUri") if isinstance(meta, dict) else None
    if not isinstance(uri, str) or not uri.startswith(UI_URI_SCHEME):
        return None
    return uri


def visibility_of(meta: Optional[Dict[str, Any]]) -> List[str]:
    """규격 기본값은 ["model", "app"]이다.

    형태가 어긋나면 기본값으로 돌아간다 — 조용히 전부 차단하면 _meta에 오타가 있는
    기존 툴이 목록에서 사라진다.
    """
    values = _ui_block(meta).get("visibility")
    if not isinstance(values, list):
        return list(_DEFAULT_VISIBILITY)
    allowed = [v for v in values if v in ("model", "app")]
    return allowed or list(_DEFAULT_VISIBILITY)


def is_model_visible(meta: Optional[Dict[str, Any]]) -> bool:
    return "model" in visibility_of(meta)


def is_app_callable(meta: Optional[Dict[str, Any]]) -> bool:
    return "app" in visibility_of(meta)
