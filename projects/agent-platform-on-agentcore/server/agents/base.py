"""
Base interface for agent clients
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, AsyncIterator, Optional


class AgentClient(ABC):
    """Base interface for all agent clients"""
    
    @abstractmethod
    async def execute_stream(
        self,
        thread_id: str,
        values: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
        actor_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Execute agent workflow and stream results

        Args:
            thread_id: Thread identifier
            values: Thread state values (including messages)
            config: Optional configuration (model_id, temperature, etc.). May
                carry a server-set "skip_recall" flag on a thread's first turn.
            actor_id: Caller identity, used to scope AgentCore Memory

        Yields:
            Dict containing event type and data
        """
        pass





