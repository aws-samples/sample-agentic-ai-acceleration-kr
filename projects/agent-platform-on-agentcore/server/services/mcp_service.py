"""
MCP service for handling MCP server connections and tool operations
API route specific service layer
"""
import logging
from typing import Dict, Any, Optional
from mcp_core import MCPClientManager, MCPUtils

logger = logging.getLogger(__name__)


class MCPService:
    """Service for MCP operations"""
    
    @staticmethod
    def create_mcp_client_from_config(server_name: str, server_config: Dict[str, Any]) -> Optional[Any]:
        """
        Create an MCP client from server configuration using MCPClientManager
        
        Args:
            server_name: Name of the MCP server
            server_config: MCP server configuration
            
        Returns:
            MCPClient instance or None if creation failed
        """
        try:
            from strands.tools.mcp import MCPClient
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import stdio_client, StdioServerParameters
        except ImportError as e:
            logger.warning(f"MCP dependencies not available: {e}")
            return None
        
        return MCPClientManager._create_single_client(
            server_name, server_config, MCPClient, streamablehttp_client, 
            stdio_client, StdioServerParameters
        )
    
    # Delegate to MCPUtils for common operations
    # These are shared utilities used by both API routes and agent execution
    get_all_tools_from_client = staticmethod(MCPUtils.get_all_tools_from_client)
    extract_tool_info = staticmethod(MCPUtils.extract_tool_info)
    call_tool = staticmethod(MCPUtils.call_tool)

