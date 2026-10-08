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
    
    # Inference defaults
    DEFAULT_MAX_TOKENS = 4096
    DEFAULT_TEMPERATURE = 0.7
    DEFAULT_REASONING_TEMPERATURE = 1.0
    MIN_REASONING_BUDGET = 1024
    
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
    
    @staticmethod
    def get_inference_config(
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        max_reasoning_length: Optional[int] = None,
    ) -> dict:
        """
        Build inference configuration for Bedrock
        
        Args:
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            max_reasoning_length: Maximum reasoning tokens (enables thinking mode)
            
        Returns:
            Dictionary with inference configuration
        """
        reasoning_enabled = max_reasoning_length is not None and max_reasoning_length > 0
        
        config = {
            "maxTokens": max_tokens if max_tokens is not None else AgentDefaults.DEFAULT_MAX_TOKENS
        }
        
        # When thinking is enabled, temperature must be set to 1.0
        if reasoning_enabled:
            config["temperature"] = AgentDefaults.DEFAULT_REASONING_TEMPERATURE
        elif temperature is not None:
            config["temperature"] = temperature
        else:
            config["temperature"] = AgentDefaults.DEFAULT_TEMPERATURE
        
        return config
    
    @staticmethod
    def get_reasoning_config(max_reasoning_length: Optional[int] = None) -> Optional[dict]:
        """
        Build reasoning configuration for Bedrock thinking mode
        
        Args:
            max_reasoning_length: Maximum reasoning tokens
            
        Returns:
            Reasoning configuration dict or None if disabled
        """
        if max_reasoning_length is None or max_reasoning_length <= 0:
            return None
        
        budget_tokens = max(max_reasoning_length, AgentDefaults.MIN_REASONING_BUDGET)
        
        return {
            "thinking": {
                "type": "enabled",
                "budget_tokens": budget_tokens
            }
        }
