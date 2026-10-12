"""
MCP utility functions for common MCP operations
Shared by both API routes and agent execution
"""
import logging
import uuid
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)


class MCPUtils:
    """Utility functions for MCP operations"""
    
    @staticmethod
    def get_all_tools_from_client(client) -> List[Any]:
        """
        Get all tools from MCP client with pagination support
        
        Args:
            client: MCPClient instance
            
        Returns:
            List of tool objects
        """
        more_tools = True
        all_tools = []
        pagination_token = None
        
        while more_tools:
            try:
                if pagination_token is not None:
                    tmp_tools = client.list_tools_sync(pagination_token=pagination_token)
                else:
                    tmp_tools = client.list_tools_sync()
                
                # Handle different return types
                tools_list = []
                if isinstance(tmp_tools, list):
                    tools_list = tmp_tools
                elif hasattr(tmp_tools, 'tools'):
                    tools_list = tmp_tools.tools if isinstance(tmp_tools.tools, list) else [tmp_tools.tools]
                elif hasattr(tmp_tools, '__iter__') and not isinstance(tmp_tools, (str, bytes)):
                    try:
                        tools_list = list(tmp_tools)
                    except:
                        tools_list = [tmp_tools]
                else:
                    tools_list = [tmp_tools]
                
                all_tools.extend(tools_list)
                
                # Check for pagination token
                if hasattr(tmp_tools, 'pagination_token'):
                    pagination_token = tmp_tools.pagination_token
                    more_tools = pagination_token is not None
                else:
                    more_tools = False
                    
            except TypeError as e:
                if pagination_token is not None:
                    pagination_token = None
                    continue
                else:
                    logger.warning(f"Error calling list_tools_sync: {e}")
                    more_tools = False
            except Exception as e:
                logger.warning(f"Error during pagination: {e}")
                if pagination_token is None and len(all_tools) == 0:
                    try:
                        tmp_tools = client.list_tools_sync()
                        if isinstance(tmp_tools, list):
                            all_tools = tmp_tools
                        elif hasattr(tmp_tools, 'tools'):
                            all_tools = tmp_tools.tools if isinstance(tmp_tools.tools, list) else [tmp_tools.tools]
                        else:
                            all_tools = [tmp_tools] if tmp_tools else []
                    except Exception as e2:
                        logger.error(f"Failed to get tools without pagination: {e2}")
                more_tools = False
        
        return all_tools
    
    @staticmethod
    def extract_tool_info(tool: Any) -> Tuple[
        Optional[str], Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]
    ]:
        """
        Extract tool information from a tool object

        Args:
            tool: Tool object (MCPAgentTool or similar)

        Returns:
            Tuple of (tool_name, description, input_schema, meta)
        """
        tool_name = None
        description = None
        input_schema = None
        meta = None

        try:
            # Get tool name
            if hasattr(tool, 'tool_name'):
                tool_name = getattr(tool, 'tool_name')
            elif hasattr(tool, 'name'):
                tool_name = getattr(tool, 'name')
            
            # MCPAgentTool wraps the actual MCP tool in 'mcp_tool' or 'tool_spec'
            mcp_tool_obj = None
            if hasattr(tool, 'mcp_tool'):
                mcp_tool_obj = getattr(tool, 'mcp_tool')
            elif hasattr(tool, 'tool_spec'):
                mcp_tool_obj = getattr(tool, 'tool_spec')
            
            # Extract from mcp_tool object
            if mcp_tool_obj:
                # Get description
                if hasattr(mcp_tool_obj, 'description'):
                    description = getattr(mcp_tool_obj, 'description')
                elif hasattr(mcp_tool_obj, 'desc'):
                    description = getattr(mcp_tool_obj, 'desc')
                
                # Get input schema
                if hasattr(mcp_tool_obj, 'inputSchema'):
                    input_schema = getattr(mcp_tool_obj, 'inputSchema')
                elif hasattr(mcp_tool_obj, 'input_schema'):
                    input_schema = getattr(mcp_tool_obj, 'input_schema')
                elif hasattr(mcp_tool_obj, 'schema'):
                    input_schema = getattr(mcp_tool_obj, 'schema')
                elif hasattr(mcp_tool_obj, 'parameters'):
                    input_schema = getattr(mcp_tool_obj, 'parameters')
                
                # If mcp_tool is a Pydantic model, try model_dump
                if (not description or not input_schema) and hasattr(mcp_tool_obj, 'model_dump'):
                    try:
                        mcp_tool_data = mcp_tool_obj.model_dump(exclude_none=False, mode='python', by_alias=True)
                        if not description:
                            description = mcp_tool_data.get('description') or mcp_tool_data.get('desc')
                        if not input_schema:
                            input_schema = mcp_tool_data.get('inputSchema') or mcp_tool_data.get('input_schema') or mcp_tool_data.get('schema') or mcp_tool_data.get('parameters')
                    except Exception:
                        pass
            
            # Fallback to direct attributes on tool
            if not description:
                if hasattr(tool, 'description'):
                    description = getattr(tool, 'description')
                elif hasattr(tool, 'desc'):
                    description = getattr(tool, 'desc')
            
            if not input_schema:
                if hasattr(tool, 'inputSchema'):
                    input_schema = getattr(tool, 'inputSchema')
                elif hasattr(tool, 'input_schema'):
                    input_schema = getattr(tool, 'input_schema')
                elif hasattr(tool, 'schema'):
                    input_schema = getattr(tool, 'schema')
                elif hasattr(tool, 'parameters'):
                    input_schema = getattr(tool, 'parameters')
            
            # Try Pydantic model methods as last resort
            if not tool_name or (not description and not input_schema):
                try:
                    if hasattr(tool, 'model_dump'):
                        try:
                            tool_data = tool.model_dump(exclude_none=False, mode='python', by_alias=True)
                        except:
                            try:
                                tool_data = tool.model_dump(exclude_none=False, by_alias=True)
                            except:
                                tool_data = tool.model_dump(exclude_none=False)
                        
                        if tool_data:
                            if not tool_name:
                                tool_name = tool_data.get('tool_name') or tool_data.get('name')
                            if not description:
                                description = tool_data.get('description') or tool_data.get('desc')
                            if not input_schema:
                                input_schema = tool_data.get('inputSchema') or tool_data.get('input_schema') or tool_data.get('schema') or tool_data.get('parameters')
                    elif hasattr(tool, 'dict'):
                        try:
                            tool_data = tool.dict(exclude_none=False, by_alias=True)
                        except:
                            tool_data = tool.dict()
                        
                        if tool_data:
                            if not tool_name:
                                tool_name = tool_data.get('tool_name') or tool_data.get('name')
                            if not description:
                                description = tool_data.get('description') or tool_data.get('desc')
                            if not input_schema:
                                input_schema = tool_data.get('inputSchema') or tool_data.get('input_schema') or tool_data.get('schema') or tool_data.get('parameters')
                except Exception:
                    pass
            
            # Convert input_schema to dict if needed
            if input_schema and not isinstance(input_schema, dict):
                try:
                    if hasattr(input_schema, 'model_dump'):
                        input_schema = input_schema.model_dump()
                    elif hasattr(input_schema, 'dict'):
                        input_schema = input_schema.dict()
                    elif hasattr(input_schema, '__dict__'):
                        input_schema = {k: v for k, v in input_schema.__dict__.items() 
                                      if not k.startswith('_') and not callable(v)}
                    else:
                        input_schema = None
                except Exception:
                    input_schema = None

            # _meta 추출. Strands MCPAgentTool.tool_spec은 _meta를 버리므로 원본
            # mcp_tool에서만 읽을 수 있다.
            try:
                source = getattr(tool, "mcp_tool", None) or tool
                candidate = getattr(source, "meta", None)
                if candidate is None:
                    candidate = getattr(source, "_meta", None)
                if isinstance(candidate, dict):
                    meta = candidate
            except Exception:
                pass

        except Exception as e:
            logger.warning(f"Failed to extract tool info: {e}")

        return tool_name, description, input_schema, meta
    
    @staticmethod
    def call_tool(mcp_client: Any, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call an MCP tool
        
        Args:
            mcp_client: Initialized MCPClient instance
            tool_name: Name of the tool to call
            arguments: Tool arguments
            
        Returns:
            Tool result as dictionary
        """
        tool_use_id = f"tool-{uuid.uuid4().hex[:8]}"
        
        logger.info(f"Calling tool: {tool_name} with {len(arguments)} arguments")
        result = mcp_client.call_tool_sync(
            tool_use_id=tool_use_id,
            name=tool_name,
            arguments=arguments
        )
        
        # Convert result to JSON-serializable format
        result_dict = None
        
        try:
            if hasattr(result, 'model_dump'):
                result_dict = result.model_dump()
            elif hasattr(result, 'dict'):
                result_dict = result.dict()
            elif isinstance(result, dict):
                result_dict = result
            else:
                # Try direct attribute access
                result_dict = {}
                if hasattr(result, 'content'):
                    content = getattr(result, 'content')
                    if content:
                        content_list = []
                        for item in content:
                            if hasattr(item, 'model_dump'):
                                content_list.append(item.model_dump())
                            elif hasattr(item, 'dict'):
                                content_list.append(item.dict())
                            elif isinstance(item, dict):
                                content_list.append(item)
                            else:
                                item_dict = {}
                                if hasattr(item, 'type'):
                                    item_dict['type'] = getattr(item, 'type')
                                if hasattr(item, 'text'):
                                    item_dict['text'] = getattr(item, 'text')
                                elif hasattr(item, 'data'):
                                    item_dict['data'] = getattr(item, 'data')
                                else:
                                    item_dict = {"type": "unknown", "raw": str(item)}
                                content_list.append(item_dict)
                        result_dict['content'] = content_list
                else:
                    result_dict = {"raw": str(result)}
        except Exception as e:
            logger.warning(f"Failed to extract result: {e}")
            try:
                import json
                result_dict = json.loads(json.dumps(result, default=str))
            except:
                result_dict = {"raw": str(result)}
        
        # Process content if it exists
        if 'content' in result_dict and isinstance(result_dict['content'], list):
            processed_content = []
            for item in result_dict['content']:
                if isinstance(item, dict):
                    processed_content.append(item)
                else:
                    try:
                        if hasattr(item, 'model_dump'):
                            processed_content.append(item.model_dump())
                        elif hasattr(item, 'dict'):
                            processed_content.append(item.dict())
                        else:
                            processed_content.append({"type": "unknown", "raw": str(item)})
                    except:
                        processed_content.append({"type": "unknown", "raw": str(item)})
            result_dict['content'] = processed_content
        
        logger.info(f"Tool call completed: {tool_name}")
        return result_dict

