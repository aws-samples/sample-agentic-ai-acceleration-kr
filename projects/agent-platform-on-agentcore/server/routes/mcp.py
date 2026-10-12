"""
MCP server routes for testing connections and listing tools.

Every route requires admin, and /tools and /tools/call are why: both hand the
caller's server config to MCPService.create_mcp_client_from_config, which for a
stdio transport reaches _create_stdio_client and runs the caller's `command`
and `args` verbatim. /cognito/login is a credential proxy for the caller's
client_secret. /servers only reads a preset file, but the sole consumer of this
router is the MCP Tools page, which is already admin-only in the sidebar —
gating the router as a whole leaves no route to forget later.

/records/* are a different shape: they take a registry record id and resolve the
endpoint server-side, so no caller-supplied config is executed. They could be
opened to any user (that is what /api/mcp-apps/* does with the same resolution),
and are kept admin because they are an operator's inspection surface — a raw
tool call with hand-written arguments skips the model that would normally decide
them. Non-admins still see the descriptor's static tool list in the registry panel.
"""
import logging
import traceback
import json
import os
import boto3
import requests
import base64
import hmac
import hashlib
from pathlib import Path
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Dict, Any, List, Optional
from core.auth import AuthUser, require_admin
from mcp_core import MCPClientManager
from mcp_core.errors import root_causes
from services.mcp_apps_service import shared_relay
from services.mcp_service import MCPService
from models.mcp import (
    MCPRecordToolCallRequest,
    MCPRecordToolsResponse,
    MCPServerConfigRequest,
    MCPToolInfo,
    MCPServerToolsResponse,
    MCPToolsListResponse,
    MCPToolCallRequest,
    MCPToolCallResponse,
    CognitoLoginRequest,
    CognitoLoginResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# Cache for MCP server presets
_mcp_server_presets_cache: Optional[List[Dict[str, Any]]] = None


def load_mcp_server_presets() -> List[Dict[str, Any]]:
    """
    Load MCP server presets from JSON file
    
    TODO: Replace with DynamoDB integration
    Future implementation:
    - Query DynamoDB table for MCP server presets
    - Support filtering by category, tags, etc.
    - Support pagination
    - Cache results with TTL
    """
    global _mcp_server_presets_cache
    
    if _mcp_server_presets_cache is not None:
        return _mcp_server_presets_cache
    
    try:
        # Get the path to the data directory
        current_dir = Path(__file__).parent.parent
        data_file = current_dir / "data" / "mcp-servers.json"
        
        if not data_file.exists():
            logger.warning(f"MCP servers data file not found at {data_file}")
            return []
        
        with open(data_file, 'r', encoding='utf-8') as f:
            _mcp_server_presets_cache = json.load(f)
        
        logger.info(f"Loaded {len(_mcp_server_presets_cache)} MCP server presets")
        return _mcp_server_presets_cache
    except Exception as e:
        logger.error(f"Failed to load MCP server presets: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return []


@router.get("/servers")
async def get_mcp_server_presets(
    category: Optional[str] = Query(None, description="Filter by category (aws-labs, community, official)"),
    tag: Optional[str] = Query(None, description="Filter by tag"),
    _: AuthUser = Depends(require_admin),
):
    """
    Get list of available MCP server presets
    
    Returns a list of pre-configured MCP servers that can be added to the configuration.
    Supports filtering by category and tags.
    
    TODO: Replace with DynamoDB integration
    """
    servers = load_mcp_server_presets()
    
    # Apply filters
    if category:
        servers = [s for s in servers if s.get("category") == category]
    
    if tag:
        servers = [s for s in servers if tag in s.get("tags", [])]
    
    return {
        "servers": servers,
        "count": len(servers)
    }


def load_tools_from_mcp_server(server_name: str, server_config: Dict[str, Any]) -> MCPServerToolsResponse:
    """
    Load tools from a single MCP server
    
    Args:
        server_name: Name of the MCP server
        server_config: Configuration for the MCP server
        
    Returns:
        MCPServerToolsResponse with tools or error
    """
    try:
        if not isinstance(server_config, dict):
            return MCPServerToolsResponse(
                server_name=server_name,
                url="",
                success=False,
                error=f"Invalid server config: expected dict, got {type(server_config)}"
            )
        
        # Skip disabled servers
        if server_config.get("disabled", False):
            return MCPServerToolsResponse(
                server_name=server_name,
                url="",
                success=False,
                error="Server is disabled"
            )
        
        # Determine display URL
        transport_type = server_config.get("type", "auto")
        url = server_config.get("url")
        command = server_config.get("command")
        args = server_config.get("args", [])
        
        if transport_type == "auto":
            if url:
                transport_type = "http"
            elif command:
                transport_type = "stdio"
        
        display_url = url if url else (f"{command} {' '.join(args) if isinstance(args, list) else ''}" if command else "")
        
        # Create MCP client using MCPService (which uses MCPClientManager internally)
        mcp_client = MCPService.create_mcp_client_from_config(server_name, server_config)
        
        if not mcp_client:
            return MCPServerToolsResponse(
                server_name=server_name,
                url=display_url,
                success=False,
                error="Failed to create MCP client"
            )
        
        # Initialize client
        try:
            logger.info(f"Connecting to MCP server: {server_name}")
            mcp_client.__enter__()
            logger.info(f"Successfully connected to MCP server: {server_name}")
        except Exception as init_error:
            error_msg = str(init_error)
            logger.error(f"Failed to connect to MCP server {server_name}: {error_msg}")
            return MCPServerToolsResponse(
                server_name=server_name,
                url=display_url,
                success=False,
                error=f"Connection failed: {error_msg}"
            )
        
        try:
            # Get all tools with pagination support
            tools = MCPService.get_all_tools_from_client(mcp_client)
            logger.info(f"Retrieved {len(tools)} tools from MCP server {server_name}")
            
            # Extract tool information
            tool_infos = []
            for tool in tools:
                tool_name, description, input_schema, tool_meta = MCPService.extract_tool_info(tool)

                if not tool_name:
                    logger.warning(f"Tool missing name, skipping")
                    continue

                tool_info = MCPToolInfo(
                    name=str(tool_name),
                    description=str(description) if description else None,
                    input_schema=input_schema,
                    meta=tool_meta,
                )
                tool_infos.append(tool_info)
            
            logger.info(f"Loaded {len(tool_infos)} tools from MCP server {server_name}")
            
            return MCPServerToolsResponse(
                server_name=server_name,
                url=display_url,
                success=True,
                tools=tool_infos
            )
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to load tools from MCP server {server_name}: {error_msg}")
            return MCPServerToolsResponse(
                server_name=server_name,
                url=display_url,
                success=False,
                error=error_msg
            )
        finally:
            # Always close the connection
            try:
                mcp_client.__exit__(None, None, None)
            except:
                pass
                
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error setting up MCP server {server_name}: {error_msg}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        # Try to get display info from config
        display_info = ""
        try:
            if isinstance(server_config, dict):
                url = server_config.get("url", "")
                command = server_config.get("command", "")
                args = server_config.get("args", [])
                if url:
                    display_info = url
                elif command:
                    display_info = f"{command} {' '.join(args) if isinstance(args, list) else ''}"
        except:
            pass
        
        return MCPServerToolsResponse(
            server_name=server_name,
            url=display_info,
            success=False,
            error=error_msg
        )


@router.post("/tools", response_model=MCPToolsListResponse)
async def list_mcp_tools(
    request: MCPServerConfigRequest, _: AuthUser = Depends(require_admin)
):
    """
    List all tools from configured MCP servers
    
    This endpoint tests the connection to each MCP server and returns
    the list of available tools from each server.
    """
    mcp_servers = request.mcp_servers
    
    if not mcp_servers:
        return MCPToolsListResponse(
            total_servers=0,
            total_tools=0,
            servers=[]
        )
    
    # Normalize configuration format (handles wrapped format)
    mcp_servers = MCPClientManager.normalize_config(mcp_servers)
    
    server_responses = []
    total_tools = 0
    
    for server_name, server_config in mcp_servers.items():
        response = load_tools_from_mcp_server(server_name, server_config)
        server_responses.append(response)
        if response.success:
            total_tools += len(response.tools)
    
    logger.info(f"Total: {total_tools} tools from {len(server_responses)} servers")
    
    return MCPToolsListResponse(
        total_servers=len(server_responses),
        total_tools=total_tools,
        servers=server_responses
    )


@router.post("/tools/call", response_model=MCPToolCallResponse)
async def call_mcp_tool(
    request: MCPToolCallRequest, _: AuthUser = Depends(require_admin)
):
    """
    Call a specific tool from an MCP server
    
    This endpoint connects to the specified MCP server and executes
    the requested tool with the provided arguments.
    """
    server_name = request.server_name
    server_config = request.server_config
    tool_name = request.tool_name
    arguments = request.arguments
    
    # Validate inputs
    if not tool_name or not isinstance(tool_name, str):
        return MCPToolCallResponse(
            success=False,
            error=f"Invalid tool_name: must be a non-empty string, got {type(tool_name)}"
        )
    
    if not isinstance(arguments, dict):
        arguments = arguments if isinstance(arguments, dict) else {}
    
    try:
        # Create MCP client using MCPService (which uses MCPClientManager internally)
        mcp_client = MCPService.create_mcp_client_from_config(server_name, server_config)
        
        if not mcp_client:
            return MCPToolCallResponse(
                success=False,
                error="Failed to create MCP client"
            )
        
        # Initialize client
        try:
            logger.info(f"Connecting to MCP server {server_name} to call tool: {tool_name}")
            mcp_client.__enter__()
        except Exception as e:
            return MCPToolCallResponse(
                success=False,
                error=f"Failed to connect to MCP server: {str(e)}"
            )
        
        try:
            # Call the tool
            result_dict = MCPService.call_tool(mcp_client, tool_name, arguments)
            
            logger.info(f"Tool call successful: {tool_name}")
            return MCPToolCallResponse(
                success=True,
                result=result_dict
            )
                    
        except Exception as e:
            logger.error(f"Error calling tool {tool_name}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return MCPToolCallResponse(
                success=False,
                error=f"Tool execution failed: {str(e)}"
            )
        finally:
            # Always close the connection
            try:
                mcp_client.__exit__(None, None, None)
            except:
                pass
                
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error calling MCP tool: {error_msg}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return MCPToolCallResponse(
            success=False,
            error=error_msg
        )


def _explain(cause: str) -> str:
    """Add the reason behind a 401/403 from an AgentCore endpoint.

    The platform signs these with SigV4 from the server's own credentials. Two
    different failures land here and they need different fixes, so don't assert
    one: (1) an AWS_IAM gateway rejects the SigV4 caller because the server task
    role is missing `bedrock-agentcore:InvokeGateway`; or (2) a CUSTOM_JWT gateway
    ignores the signature entirely and wants a bearer token issued for its
    authorizer, which the server has no way to mint. Without this note the reply
    is a bare "403 Forbidden" that reads like a broken deployment.
    """
    if "401" in cause or "403" in cause:
        # Explanation first: the raw cause carries an MDN link and a URL-encoded
        # ARN, so leading with it buries the one sentence that helps.
        return (
            "인증이 거부되었습니다. 서버가 SigV4로 서명해 호출했는데 거부됐습니다 — "
            "AWS_IAM 게이트웨이라면 서버 역할에 bedrock-agentcore:InvokeGateway 권한이 "
            "없는 것이고, CUSTOM_JWT 게이트웨이라면 서명 대신 발급된 bearer 토큰을 "
            f"요구하는데 이 화면에서는 발급할 수 없습니다.\n\n{cause}"
        )
    return cause


@router.get("/records/{record_id:path}/tools", response_model=MCPRecordToolsResponse)
async def list_record_tools(record_id: str, _: AuthUser = Depends(require_admin)):
    """Live tools/list against a registered MCP record.

    Goes through the MCP Apps relay rather than MCPClientManager because that is
    the only session path that signs AgentCore endpoints with SigV4 — a gateway
    or MCP-protocol runtime record answers 403 without it, and most records here
    are exactly that. The endpoint comes from the record's descriptor, never from
    the request.
    """
    try:
        endpoint, tools = shared_relay().list_tools(record_id)
    except ValueError as exc:
        # No such record, or an MCP record with no endpoint URL registered.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Live tools/list failed for record %s: %s", record_id, exc)
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=502, detail=_explain(root_causes(exc)))

    return MCPRecordToolsResponse(
        record_id=record_id,
        endpoint=endpoint,
        tools=[MCPToolInfo(**tool) for tool in tools],
    )


@router.post("/records/tools/call", response_model=MCPToolCallResponse)
async def call_record_tool(
    request: MCPRecordToolCallRequest, _: AuthUser = Depends(require_admin)
):
    """Call one tool on a registered MCP record.

    Unlike /tools/call this takes no server_config, so nothing the caller sends
    decides what gets connected to or executed.

    Errors come back as a 200 with success=False, matching /tools/call — the UI
    renders a failed tool call as a result, not as a page-level failure.
    """
    try:
        result = shared_relay().call_tool(
            request.record_id, request.tool_name, request.arguments
        )
    except ValueError as exc:
        return MCPToolCallResponse(success=False, error=str(exc))
    except Exception as exc:
        logger.error(
            "Tool call %s failed on record %s: %s",
            request.tool_name,
            request.record_id,
            exc,
        )
        logger.error(traceback.format_exc())
        return MCPToolCallResponse(success=False, error=_explain(root_causes(exc)))

    return MCPToolCallResponse(success=True, result=result)


@router.post("/cognito/login", response_model=CognitoLoginResponse)
async def cognito_login(
    request: CognitoLoginRequest, _: AuthUser = Depends(require_admin)
):
    """
    Authenticate with Cognito and get JWT access token
    
    Supports two authentication flows:
    1. USER_PASSWORD_AUTH: username + password (optionally with client_secret)
    2. CLIENT_CREDENTIALS: client_id + client_secret (Machine-to-Machine)
    """
    try:
        client_id = request.client_id
        username = request.username
        password = request.password
        client_secret = request.client_secret
        region = request.region
        auth_flow = request.auth_flow or "USER_PASSWORD_AUTH"
        
        # Validate based on auth flow
        if auth_flow == "CLIENT_CREDENTIALS":
            if not client_id or not client_secret:
                missing = []
                if not client_id: missing.append("client_id")
                if not client_secret: missing.append("client_secret")
                return CognitoLoginResponse(
                    success=False,
                    error=f"Missing required fields for CLIENT_CREDENTIALS: {', '.join(missing)}"
                )
            
            # Use OAuth2 token endpoint for client credentials flow
            user_pool_domain = request.user_pool_domain
            
            # If domain not provided, try to construct it from region
            # Format: https://{domain}.auth.{region}.amazoncognito.com/oauth2/token
            if not user_pool_domain:
                # Try to get domain from user pool using client_id
                # We can query the user pool to get its domain
                try:
                    client = boto3.client('cognito-idp', region_name=region)
                    # List user pools and find the one with this client_id
                    # This is complex, so for now we'll require domain
                    return CognitoLoginResponse(
                        success=False,
                        error="User Pool Domain is required for CLIENT_CREDENTIALS flow. Format: your-domain.auth.{region}.amazoncognito.com"
                    )
                except Exception as e:
                    return CognitoLoginResponse(
                        success=False,
                        error=f"User Pool Domain is required for CLIENT_CREDENTIALS flow: {str(e)}"
                    )
            
            # Construct token endpoint URL
            if not user_pool_domain.startswith('http'):
                # Assume it's just the domain name, construct full URL
                token_url = f"https://{user_pool_domain}/oauth2/token"
            else:
                token_url = f"{user_pool_domain}/oauth2/token" if not user_pool_domain.endswith('/oauth2/token') else user_pool_domain
            
            # Prepare client credentials request
            # Cognito M2M uses form data, not Basic Auth
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            
            # Send client_id and client_secret as form data (not in Authorization header)
            data = {
                'grant_type': 'client_credentials',
                'client_id': client_id,
                'client_secret': client_secret
            }
            
            try:
                logger.info(f"Requesting token from {token_url} using CLIENT_CREDENTIALS flow")
                response = requests.post(
                    token_url,
                    headers=headers,
                    data=urlencode(data),
                    timeout=30
                )
                
                if response.status_code == 200:
                    token_data = response.json()
                    access_token = token_data.get('access_token')
                    
                    if access_token:
                        logger.info("Successfully obtained token using CLIENT_CREDENTIALS flow")
                        return CognitoLoginResponse(
                            success=True,
                            access_token=access_token
                        )
                    else:
                        return CognitoLoginResponse(
                            success=False,
                            error="No access_token in response"
                        )
                else:
                    error_text = response.text
                    logger.error(f"Token request failed: {response.status_code} - {error_text}")
                    return CognitoLoginResponse(
                        success=False,
                        error=f"Token request failed: {response.status_code} - {error_text}"
                    )
            except Exception as e:
                logger.error(f"Error during CLIENT_CREDENTIALS authentication: {str(e)}")
                return CognitoLoginResponse(
                    success=False,
                    error=f"CLIENT_CREDENTIALS authentication failed: {str(e)}"
                )
            
        else:  # USER_PASSWORD_AUTH
            if not all([client_id, username, password]):
                missing = []
                if not client_id: missing.append("client_id")
                if not username: missing.append("username")
                if not password: missing.append("password")
                return CognitoLoginResponse(
                    success=False,
                    error=f"Missing required fields: {', '.join(missing)}"
                )
            
            # Create Cognito client
            client = boto3.client('cognito-idp', region_name=region)
            
            logger.info(f"Authenticating user {username} with Cognito client {client_id}")
            
            # Prepare auth parameters
            auth_params = {
                'USERNAME': username,
                'PASSWORD': password
            }
            
            # If client_secret is provided, include it
            # Note: When client_secret is used, we need to use SECRET_HASH
            if client_secret:
                # Create SECRET_HASH as per Cognito requirements
                message = username + client_id
                dig = hmac.new(
                    client_secret.encode('utf-8'),
                    message.encode('utf-8'),
                    hashlib.sha256
                ).digest()
                secret_hash = base64.b64encode(dig).decode()
                auth_params['SECRET_HASH'] = secret_hash
            
            # Authenticate and get tokens using USER_PASSWORD_AUTH flow
            auth_kwargs = {
                'ClientId': client_id,
                'AuthFlow': 'USER_PASSWORD_AUTH',
                'AuthParameters': auth_params
            }
            
            # Include ClientSecret if provided
            if client_secret:
                auth_kwargs['ClientSecret'] = client_secret
            
            response = client.initiate_auth(**auth_kwargs)
            
            auth_result = response['AuthenticationResult']
            access_token = auth_result['AccessToken']
            
            logger.info(f"Successfully obtained Cognito access token for user {username}")
            
            return CognitoLoginResponse(
                success=True,
                access_token=access_token
            )
        
    except Exception as e:
        error_msg = str(e)
        error_type = type(e).__name__
        
        # Handle specific Cognito exceptions
        if "NotAuthorizedException" in error_type or "NotAuthorizedException" in error_msg:
            logger.error(f"Cognito authentication failed: {error_msg}")
            return CognitoLoginResponse(
                success=False,
                error="Invalid username or password"
            )
        elif "UserNotFoundException" in error_type or "UserNotFoundException" in error_msg:
            logger.error(f"User not found: {error_msg}")
            return CognitoLoginResponse(
                success=False,
                error="User not found"
            )
        elif "UserNotConfirmedException" in error_type or "UserNotConfirmedException" in error_msg:
            logger.error(f"User not confirmed: {error_msg}")
            return CognitoLoginResponse(
                success=False,
                error="User account is not confirmed. Please confirm your account first."
            )
        else:
            logger.error(f"Error during Cognito authentication: {error_msg}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return CognitoLoginResponse(
                success=False,
                error=f"Authentication failed: {error_msg}"
            )
