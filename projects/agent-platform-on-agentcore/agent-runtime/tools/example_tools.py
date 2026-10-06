"""
Example tools for the agent runtime template.

This file contains example tools that demonstrate how to create custom tools
for your specific domain. Replace these with your own domain-specific tools.

To create a new tool:
1. Import the @tool decorator from strands
2. Define your function with proper docstring
3. Use the @tool decorator
4. Add the tool to agent_manager.py's local_tools list
"""

from strands import tool
from datetime import datetime
import json
import boto3
import os
import time
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


@tool
def wait_for_seconds(seconds: int) -> str:
    """Wait for a specified number of seconds.
    
    This tool allows the agent to pause execution for a given duration.
    Useful for waiting for external processes or timing operations.
    
    Args:
        seconds: Number of seconds to wait (recommended: 1-60 seconds)
    
    Returns:
        Completion message with actual elapsed time
    """
    if seconds < 0:
        return "Error: Wait time must be greater than 0."
    
    if seconds > 300:  # Warn if more than 5 minutes
        return f"Warning: {seconds} seconds is a very long time. Maximum 300 seconds (5 minutes) is recommended."
    
    logger.info(f"Waiting for {seconds} seconds...")
    start_time = datetime.now()
    
    time.sleep(seconds)
    
    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    
    return f"Waited {seconds} seconds (actual elapsed time: {elapsed:.2f} seconds)"


@tool
def get_system_status() -> Dict[str, Any]:
    """Get the current system status.
    
    This is an example tool that retrieves system status information.
    Replace this with your own domain-specific status checking logic.
    
    Args:
        None
    
    Returns:
        Dictionary containing system status information
    """
    try:
        # Example: Load configuration
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'config.json')
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except FileNotFoundError:
            return {"error": f"config.json not found at {config_path}"}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON in config.json: {e}"}
        
        # Example status response
        return {
            "status": "operational",
            "timestamp": datetime.now().isoformat(),
            "config_loaded": True,
            "project_name": config.get("projectName", "unknown")
        }
        
    except Exception as e:
        return {
            "error": f"Unexpected error in get_system_status: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }


@tool
def analyze_data(data_json: str) -> str:
    """Analyze provided data and return insights.
    
    This is an example tool for data analysis. Replace with your own
    domain-specific analysis logic.
    
    Args:
        data_json: JSON string containing data to analyze
    
    Returns:
        Analysis results as a string
    """
    try:
        data = json.loads(data_json)
        
        # Example analysis
        analysis = f"Analyzed data with {len(data)} items at {datetime.now().isoformat()}"
        
        return analysis
        
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON format - {str(e)}"
    except Exception as e:
        return f"Error analyzing data: {str(e)}"

