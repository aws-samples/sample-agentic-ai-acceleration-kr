"""
MCP Client Manager

Manages creation and configuration of MCP clients from configuration.
Supports both HTTP and stdio transports with proper environment variable handling.
"""
import logging
import os
import traceback
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class MCPClientManager:
    """Manages MCP client creation and configuration"""
    
    @staticmethod
    def normalize_config(mcp_servers: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize MCP server configuration format.
        
        Handles both direct and wrapped formats:
        - Direct: {"server1": {...}}
        - Wrapped: {"mcpServers": {"server1": {...}}}
        
        Args:
            mcp_servers: Raw MCP server configuration
            
        Returns:
            Normalized dictionary of server configurations
        """
        if not mcp_servers:
            return {}
        
        # Handle wrapped format: {"mcpServers": {...}}
        if "mcpServers" in mcp_servers and isinstance(mcp_servers["mcpServers"], dict):
            return mcp_servers["mcpServers"]
        
        return mcp_servers
    
    @staticmethod
    def create_clients(mcp_servers: Dict[str, Any]) -> List[Any]:
        """
        Create MCP clients from configuration.
        
        Creates MCPClient instances that can be initialized and used to extract tools.
        The caller is responsible for managing the client lifecycle (enter/exit).
        
        Args:
            mcp_servers: Dictionary of MCP server configurations
                Format options:
                1. HTTP URL: {"server1": {"url": "http://...", "apiKey": "..."}}
                2. stdio (uvx): {"server1": {"command": "uvx", "args": ["package@version"], "env": {...}}}
                3. Wrapped: {"mcpServers": {"server1": {...}}}
        
        Returns:
            List of MCPClient instances (not yet initialized)
        """
        mcp_servers = MCPClientManager.normalize_config(mcp_servers)
        
        if not mcp_servers:
            return []
        
        mcp_clients = []
        
        try:
            from strands.tools.mcp import MCPClient
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import stdio_client, StdioServerParameters
        except ImportError as e:
            logger.warning(f"MCP dependencies not available: {e}. MCP tools will not be loaded.")
            return []
        
        for server_name, server_config in mcp_servers.items():
            try:
                client = MCPClientManager._create_single_client(
                    server_name, server_config, MCPClient, streamablehttp_client, 
                    stdio_client, StdioServerParameters
                )
                if client:
                    mcp_clients.append(client)
                    logger.info(f"Created MCP client for server {server_name}")
            except Exception as e:
                logger.error(f"Error creating MCP client for server {server_name}: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                continue
        
        logger.info(f"Created {len(mcp_clients)} MCP clients")
        return mcp_clients
    
    @staticmethod
    def _create_single_client(
        server_name: str,
        server_config: Dict[str, Any],
        MCPClient: Any,
        streamablehttp_client: Any,
        stdio_client: Any,
        StdioServerParameters: Any
    ) -> Optional[Any]:
        """
        Create a single MCP client from server configuration.
        
        Args:
            server_name: Name of the MCP server
            server_config: Configuration for the server
            MCPClient: MCPClient class
            streamablehttp_client: HTTP client factory
            stdio_client: stdio client factory
            StdioServerParameters: StdioServerParameters class
            
        Returns:
            MCPClient instance or None if creation failed
        """
        if not isinstance(server_config, dict):
            logger.warning(f"Invalid MCP server config for {server_name}: expected dict, got {type(server_config)}")
            return None
        
        # Skip disabled servers
        if server_config.get("disabled", False):
            logger.info(f"Skipping disabled MCP server: {server_name}")
            return None
        
        # Determine transport type
        transport_type = server_config.get("type", "auto")
        url = server_config.get("url")
        command = server_config.get("command")
        
        # Auto-detect transport type if not explicitly set
        if transport_type == "auto":
            if url:
                transport_type = "http"
            elif command:
                transport_type = "stdio"
            else:
                logger.warning(f"MCP server {server_name} missing 'url' or 'command' in configuration")
                return None
        
        if transport_type == "http" or url:
            return MCPClientManager._create_http_client(
                server_name, server_config, url, MCPClient, streamablehttp_client
            )
        elif transport_type == "stdio":
            return MCPClientManager._create_stdio_client(
                server_name, server_config, command, MCPClient, stdio_client, StdioServerParameters
            )
        else:
            logger.warning(f"MCP server {server_name} has unsupported transport type: {transport_type}")
            return None
    
    @staticmethod
    def _create_http_client(
        server_name: str,
        server_config: Dict[str, Any],
        url: str,
        MCPClient: Any,
        streamablehttp_client: Any
    ) -> Optional[Any]:
        """Create HTTP-based MCP client"""
        if not url:
            logger.warning(f"MCP server {server_name} missing 'url' for HTTP transport")
            return None
        
        # Build headers with optional API key
        headers = {}
        api_key = server_config.get("apiKey") or server_config.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        
        # Add any additional headers from config
        if "headers" in server_config and isinstance(server_config["headers"], dict):
            headers.update(server_config["headers"])
        
        logger.info(f"Creating MCP client for server {server_name} with HTTP transport at {url}")
        
        # Create a factory function that properly captures variables
        # This ensures proper closure for each server
        def create_http_transport():
            """Factory function to create HTTP transport with proper closure"""
            return streamablehttp_client(
                url=url,
                headers=headers if headers else None
            )
        
        # Create MCP client with HTTP transport factory
        return MCPClient(create_http_transport)
    
    @staticmethod
    def _create_stdio_client(
        server_name: str,
        server_config: Dict[str, Any],
        command: str,
        MCPClient: Any,
        stdio_client: Any,
        StdioServerParameters: Any
    ) -> Optional[Any]:
        """Create stdio-based MCP client with environment variable support"""
        if not command:
            logger.warning(f"MCP server {server_name} missing 'command' for stdio transport")
            return None
        
        args = server_config.get("args", [])
        if not isinstance(args, list):
            logger.warning(f"MCP server {server_name} 'args' must be a list")
            return None
        
        # Verify command exists (basic check)
        import shutil
        if not shutil.which(command):
            logger.warning(f"MCP server {server_name}: Command '{command}' not found in PATH")
            return None
        
        # Get environment variables if provided
        env = server_config.get("env", {})
        if not isinstance(env, dict):
            env = {}
        
        logger.info(f"Creating MCP client for server {server_name} with stdio transport: {command} {args}")
        
        # Create StdioServerParameters
        # Note: StdioServerParameters may not support env parameter directly
        # We'll create a factory function that captures the environment variables
        # This ensures proper closure for the lambda function
        
        # Merge environment variables with current environment
        merged_env = {**os.environ, **env} if env else None
        
        # Create a factory function that properly captures variables
        # Capture command, args, and env in closure
        def create_stdio_transport():
            """Factory function to create stdio transport with proper closure"""
            # Create StdioServerParameters fresh each time
            # Try to pass env parameter if supported
            try:
                # Some versions of StdioServerParameters may support env parameter
                if merged_env:
                    stdio_params = StdioServerParameters(
                        command=command,
                        args=args,
                        env=merged_env
                    )
                else:
                    stdio_params = StdioServerParameters(
                        command=command,
                        args=args
                    )
            except TypeError:
                # Fallback: create without env parameter
                stdio_params = StdioServerParameters(
                    command=command,
                    args=args
                )
                # Try to set as attribute if available
                if merged_env and hasattr(stdio_params, 'env'):
                    stdio_params.env = merged_env
                elif env and hasattr(stdio_params, 'env_vars'):
                    stdio_params.env_vars = env
            
            return stdio_client(stdio_params)
        
        # Create MCP client with stdio transport factory
        return MCPClient(create_stdio_transport)

