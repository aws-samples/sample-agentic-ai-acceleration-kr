"""
Common models used across the application
"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Union
from datetime import datetime
from enum import Enum


class ThreadStatus(str, Enum):
    """Thread status enum"""
    IDLE = "idle"
    BUSY = "busy"
    INTERRUPTED = "interrupted"
    ERROR = "error"


class MessageType(str, Enum):
    """Message type enum"""
    HUMAN = "human"
    AI = "ai"
    SYSTEM = "system"
    TOOL = "tool"


class MessageContent(BaseModel):
    """Message content block"""
    text: Optional[str] = None
    type: Optional[str] = None

    class Config:
        extra = "allow"


class Message(BaseModel):
    """Message model"""
    id: str
    type: MessageType
    content: Union[str, List[Union[str, MessageContent]]]
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class Checkpoint(BaseModel):
    """Checkpoint model"""
    v: int = Field(default=1, alias="v")
    id: str
    ts: str  # timestamp
    channel_values: Dict[str, Any] = Field(default_factory=dict)
    channel_versions: Dict[str, Any] = Field(default_factory=dict)
    versions_seen: Dict[str, Any] = Field(default_factory=dict)


class StreamConfig(BaseModel):
    """Stream configuration"""
    recursion_limit: Optional[int] = None
    interrupt_before: Optional[List[str]] = None
    interrupt_after: Optional[List[str]] = None
    # Per-turn overrides for a harness agent. InvokeHarness accepts `model` and
    # `systemPrompt` per request (measured 2026-09-23; an older comment here
    # claimed it did not) and applies them to that turn only — the harness
    # definition is untouched and no version is created. InvokeAgentRuntime has
    # no such fields; the runtime path forwards both inside the payload instead
    # (agent-runtime/main.py reads `system_prompt` and `model_id`). The web keeps
    # them per thread in Thread.metadata["harness_overrides"] and resends them
    # each turn; basic chat sets model_id server-side from the allow-list.
    system_prompt: Optional[str] = None
    model_id: Optional[str] = None
    # AgentCore Runtime target
    agent_runtime_arn: Optional[str] = None  # ARN for AgentCore Runtime
    # Managed harness target; invoked via InvokeHarness instead of the runtime API.
    harness_arn: Optional[str] = None
    qualifier: Optional[str] = None  # Qualifier (version/alias) for AgentCore Runtime
    # Registry record the chat is bound to. Enforced, not advisory: the first
    # turn pins it onto the thread and later turns must match, because AgentCore
    # Memory is scoped by a session id derived from the thread id alone. See
    # Thread.agent_record_id and ThreadService._pin_agent.
    registry_record_id: Optional[str] = None
    # The record's name, carried only so the thread can be labelled without a
    # registry lookup on every turn. Client-supplied and therefore never trusted
    # for a decision — only registry_record_id gates which agent may answer.
    registry_agent_name: Optional[str] = None
    # Basic chat: no registry agent, the default runtime answers with one of the
    # operator-allowed models. The server binds the target itself; the ARNs the
    # client may have sent are not consulted. See StreamingService._bind_basic_chat.
    basic_chat: bool = False
    basic_chat_model_id: Optional[str] = None
    
    class Config:
        extra = "allow"  # Allow extra fields that are not defined


class StreamCommand(BaseModel):
    """Stream command"""
    resume: Optional[List[Dict[str, Any]]] = None
    goto: Optional[str] = None
    update: Optional[Any] = None


class StreamRequest(BaseModel):
    """Stream request"""
    values: Optional[Dict[str, Any]] = None
    config: Optional[StreamConfig] = None
    checkpoint: Optional[Checkpoint] = None
    command: Optional[StreamCommand] = None
    interrupt_before: Optional[List[str]] = None
    interrupt_after: Optional[List[str]] = None


class StreamEvent(BaseModel):
    """Stream event for SSE"""
    event: str
    data: Dict[str, Any]

    def model_dump(self, **kwargs):
        """Override to use dict() for compatibility"""
        return {
            "event": self.event,
            "data": self.data,
        }

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class StateType(BaseModel):
    """State type for streaming"""
    messages: Optional[List[Message]] = None
    todos: Optional[List[Dict[str, Any]]] = None
    files: Optional[Dict[str, str]] = None
    email: Optional[Dict[str, Any]] = None
    ui: Optional[Any] = None

