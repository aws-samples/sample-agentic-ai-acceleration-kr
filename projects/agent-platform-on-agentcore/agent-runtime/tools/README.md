# Tools Directory

This directory contains agent tools that can be used by the Strands agent.

## Structure

- **`example_tools.py`**: Template tools showing how to create custom tools for your domain

## Creating Your Own Tools

### Basic Tool Template

```python
from strands import tool
from typing import Any, Dict

@tool
def your_tool_name(param1: str, param2: int) -> Dict[str, Any]:
    """Tool description that explains what this tool does.
    
    Args:
        param1: Description of param1
        param2: Description of param2
    
    Returns:
        Dictionary containing the result
    """
    # Your implementation here
    return {
        "status": "success",
        "result": param1 + str(param2)
    }
```

### Tool Registration

After creating your tools, register them in `core/agent_manager.py`:

```python
from tools.your_tools import your_tool_name

local_tools = [
    your_tool_name,
    # Add more tools...
]
```

## Best Practices

1. **Clear Documentation**: Always provide detailed docstrings
2. **Type Hints**: Use type hints for better IDE support and error checking
3. **Error Handling**: Handle errors gracefully and return meaningful error messages
4. **Logging**: Use logging for debugging and monitoring
5. **Idempotency**: Make tools idempotent when possible

## Example Tools

See `example_tools.py` for examples of:
- Simple utility tools (`wait_for_seconds`)
- Status checking tools (`get_system_status`)
- Data analysis tools (`analyze_data`)

