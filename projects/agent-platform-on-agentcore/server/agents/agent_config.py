"""
Configuration management for agent clients
"""
import os
from typing import Optional
from dataclasses import dataclass


@dataclass
class AgentDefaults:
    """Default values for agent configuration"""
    
    # Model defaults
    DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-5-5"
    DEFAULT_REGION = "ap-northeast-1"
    
    # AgentCore defaults
    DEFAULT_QUALIFIER = "DEFAULT"


class AgentConfig:
    """Configuration loader for agent clients"""
    
    @staticmethod
    def get_model_id(override: Optional[str] = None) -> str:
        """Get model ID from override, environment, or default"""
        return override or os.getenv(
            "BEDROCK_MODEL_ID",
            AgentDefaults.DEFAULT_MODEL_ID
        )
    
    @staticmethod
    def get_region(override: Optional[str] = None) -> str:
        """Get AWS region from override, environment, or default"""
        return override or os.getenv(
            "AWS_REGION",
            AgentDefaults.DEFAULT_REGION
        )
    
    @staticmethod
    def get_agent_runtime_arn(override: Optional[str] = None) -> str:
        """Get AgentCore Runtime ARN from override or environment"""
        return override or os.getenv("AGENT_RUNTIME_ARN", "")
    
    @staticmethod
    def get_qualifier(override: Optional[str] = None) -> str:
        """Get AgentCore qualifier from override, environment, or default"""
        return override or os.getenv(
            "AGENT_RUNTIME_QUALIFIER",
            AgentDefaults.DEFAULT_QUALIFIER
        )
