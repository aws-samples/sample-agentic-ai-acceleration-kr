"""
MCP-related Pydantic models for API requests and responses
"""
from pydantic import BaseModel
from typing import Dict, Any, Optional, List

from core.config import COGNITO_REGION


class MCPServerConfigRequest(BaseModel):
    """Request model for MCP server configuration"""
    mcp_servers: Dict[str, Any]


class MCPToolInfo(BaseModel):
    """Information about an MCP tool"""
    name: str
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    # MCP Apps(_meta.ui.resourceUri)가 여기로 흐른다. 이 필드가 없어서 UI 메타가
    # 조용히 유실됐고 앱이 렌더링되지 않았다.
    meta: Optional[Dict[str, Any]] = None


class MCPServerToolsResponse(BaseModel):
    """Response model for MCP server tools"""
    server_name: str
    url: str
    success: bool
    tools: List[MCPToolInfo] = []
    error: Optional[str] = None


class MCPToolsListResponse(BaseModel):
    """Response model for listing all MCP tools"""
    total_servers: int
    total_tools: int
    servers: List[MCPServerToolsResponse] = []


class MCPToolCallRequest(BaseModel):
    """Request model for calling an MCP tool"""
    server_name: str
    server_config: Dict[str, Any]
    tool_name: str
    arguments: Dict[str, Any] = {}


class MCPToolCallResponse(BaseModel):
    """Response model for MCP tool call"""
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None


class MCPRecordToolsResponse(BaseModel):
    """Tools served by a registered MCP record.

    `endpoint` is echoed so an admin can see which server answered; the caller
    never supplies it, since the record id is the only input (see routes/mcp.py
    for why that matters and why the route is still admin-gated).
    """
    record_id: str
    endpoint: str
    tools: List[MCPToolInfo] = []


class MCPRecordToolCallRequest(BaseModel):
    """Call a tool on a registered MCP record. No server_config, by design."""
    record_id: str
    tool_name: str
    arguments: Dict[str, Any] = {}


class CognitoLoginRequest(BaseModel):
    """Request model for Cognito login"""
    client_id: str
    username: Optional[str] = None
    password: Optional[str] = None
    client_secret: Optional[str] = None
    user_pool_domain: Optional[str] = None  # Required for CLIENT_CREDENTIALS flow
    # Defaults to the pool's configured region. A hardcoded us-east-1 meant that
    # a deployment whose pool lives elsewhere failed here with
    # ResourceNotFoundException whenever the caller omitted the field, even
    # though COGNITO_REGION was set correctly for every other auth path.
    region: str = COGNITO_REGION
    auth_flow: str = "USER_PASSWORD_AUTH"  # USER_PASSWORD_AUTH or CLIENT_CREDENTIALS


class CognitoLoginResponse(BaseModel):
    """Response model for Cognito login"""
    success: bool
    access_token: Optional[str] = None
    error: Optional[str] = None

