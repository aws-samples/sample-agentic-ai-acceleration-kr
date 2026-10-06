"""MCP Apps 릴레이 요청/응답 모델.

여기에 `server_config`가 없는 것이 의도다. 기존 /api/mcp/* 는 브라우저가 보낸 설정으로
stdio 명령을 실행하기 때문에 require_admin이다. 앱 발신 호출은 일반 사용자가
발생시키므로, 엔드포인트는 레지스트리 record_id로만 지정한다.
"""
from typing import Any, Dict, Optional

from pydantic import BaseModel


class McpAppResourceReadRequest(BaseModel):
    record_id: str
    uri: str


class McpAppResourceReadResponse(BaseModel):
    success: bool
    uri: Optional[str] = None
    mime_type: Optional[str] = None
    text: Optional[str] = None
    ui_meta: Dict[str, Any] = {}
    error: Optional[str] = None


class McpAppToolCallRequest(BaseModel):
    record_id: str
    tool_name: str
    arguments: Dict[str, Any] = {}


class McpAppToolCallResponse(BaseModel):
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None


class McpAppToolDescribeRequest(BaseModel):
    record_id: str
    tool_name: str


class McpAppToolDescribeResponse(BaseModel):
    success: bool
    tool: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
