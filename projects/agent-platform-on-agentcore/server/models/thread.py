"""
Thread models
"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from .common import ThreadStatus


class Thread(BaseModel):
    """Thread model"""
    thread_id: str
    created_at: str
    updated_at: str
    values: Optional[Dict[str, Any]] = Field(default_factory=dict)
    status: ThreadStatus = ThreadStatus.IDLE
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    # The Cognito `sub` of the caller who created the thread; see
    # ThreadService.require_owned. Its own field rather than a key inside
    # `metadata` for two reasons: PATCH /state merges caller-supplied metadata
    # into the thread, so a user could forge ownership, and `metadata` is stored
    # as a JSON string, which DynamoDB cannot filter the thread list on.
    #
    # Empty means a record written before ownership existed. Those fail closed,
    # so treat "" as "nobody", never as "everybody".
    owner_sub: str = ""
    # The registry record whose agent answers in this thread. Pinned on the first
    # turn and enforced from then on; see ThreadService.get_or_create_thread.
    #
    # This is a correctness field, not a display convenience. AgentCore Memory
    # events are scoped by (memory_id, actor_id, session_id), and session_id is
    # derived from the thread id alone (AgentCoreClient._session_id) — the agent
    # is not part of the scope. So letting two agents share a thread either mixes
    # their turns into one event stream (runtime agents deployed from this repo
    # share a single MEMORY_ID) or strands the history in the other's memory
    # (a harness owns its own managed memory). Pinning the agent is what makes
    # the thread-derived session id sound.
    #
    # A top-level field rather than a key inside `metadata`, for the same two
    # reasons as owner_sub: PATCH /state merges caller-supplied metadata, so a
    # user could repoint the thread, and `metadata` is stored as a JSON string,
    # which DynamoDB cannot filter on.
    agent_record_id: str = ""
    # The agent's name as it was when the thread was pinned, so the sidebar can
    # label a thread without resolving every record — and still reads correctly
    # after the record is renamed or deleted. Never authoritative: only
    # agent_record_id decides which agent may answer.
    agent_name: str = ""
    # Model the retired basic-chat path pinned onto its threads. Read once, on
    # the turn that moves such a thread onto the default agent's record, then
    # cleared (ThreadService._adopt_default_agent). Never written any more.
    basic_chat_model_id: Optional[str] = None
    # Execution target (agent_runtime_arn / harness_arn / qualifier) derived from
    # the pinned record the last time the registry was consulted. A cache, not a
    # decision: services/agent_access.py re-reads the record whenever the client
    # sends something else. Top-level for the same reason as agent_record_id.
    agent_target: Optional[Dict[str, Optional[str]]] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class ThreadSearchRequest(BaseModel):
    """Thread search request"""
    limit: Optional[int] = 20
    offset: Optional[int] = 0
    sort_by: Optional[str] = "updated_at"
    sort_order: Optional[str] = "desc"
    status: Optional[ThreadStatus] = None
    metadata: Optional[Dict[str, Any]] = None


class ThreadSearchResponse(BaseModel):
    """Thread search response"""
    threads: List[Thread]


class ThreadStateUpdate(BaseModel):
    """Thread state update request"""
    values: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

