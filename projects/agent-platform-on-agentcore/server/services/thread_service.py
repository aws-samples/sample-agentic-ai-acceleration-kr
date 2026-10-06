"""
Thread service for business logic
"""
import uuid
from typing import Dict, Any, List, Optional
from datetime import datetime

from repositories.thread_repository import ThreadRepository
from models.thread import Thread, ThreadStateUpdate, ThreadStatus
from models.common import ThreadStatus as ThreadStatusEnum
from core.config import BASIC_CHAT_RECORD_ID


class ThreadNotFound(Exception):
    """No thread with that id."""


class ThreadForbidden(Exception):
    """The thread exists but belongs to another user."""


class ThreadAgentMismatch(Exception):
    """The thread is pinned to a different agent than the one being invoked.

    Distinct from ThreadForbidden because it is not an entitlement problem: the
    caller owns the thread. It is a state conflict, and the fix is a new thread
    rather than different credentials — so the route answers 409, not 403.
    """


class ThreadService:
    """Service for thread business logic"""

    def __init__(self, repository: ThreadRepository):
        self.repository = repository

    def require_owned(
        self, thread_id: str, owner_sub: str, is_admin: bool = False
    ) -> Thread:
        """The thread, if this caller may use it. Otherwise it raises.

        The single ownership check behind every thread route, and behind the
        artifact and attachment routes that address data through a thread id.

        `ThreadForbidden` (403) rather than 404 on a mismatch, matching
        `require_admin`: the caller is known, just not entitled. It does confirm
        the id exists, which is an accepted trade for a debuggable boundary.

        Admin passes, read and write alike, as it does for knowledge bases
        (`KnowledgeService._readable`/`_writable`) — one rule across the
        platform's user data rather than a different answer per feature.

        `is_admin` is a parameter rather than something read off an AuthUser
        here, so the streaming path can pass False: see get_or_create_thread.
        """
        thread = self.repository.get(thread_id)
        if not thread:
            raise ThreadNotFound(f"Thread {thread_id} not found")
        if is_admin:
            return thread
        # An absent owner is nobody, not everybody: records written before
        # ownership existed must not stay readable by any account. The backfill
        # script (scripts/backfill_thread_owner.py) is how they get an owner.
        if not thread.owner_sub or thread.owner_sub != owner_sub:
            raise ThreadForbidden(f"Thread {thread_id} belongs to another user")
        return thread

    def get_thread(self, thread_id: str) -> Optional[Thread]:
        """Get thread by ID, with no ownership check.

        Internal reads only — routes must go through require_owned.
        """
        return self.repository.get(thread_id)

    def search_threads(
        self,
        owner_sub: Optional[str],
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        status: Optional[ThreadStatusEnum] = None,
        metadata: Optional[Dict[str, Any]] = None,
        agent_record_id: Optional[str] = None,
    ) -> List[Thread]:
        """The caller's own threads, or everyone's when `owner_sub` is None.

        `agent_record_id` narrows to the threads one agent answered in; the
        insights drill-down needs that list to fetch a span timeline or start an
        evaluation, and neither the registry nor the usage counters hold it.
        """
        return self.repository.search(
            owner_sub=owner_sub,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
            status=status,
            metadata=metadata,
            agent_record_id=agent_record_id,
        )

    def create_thread(
        self,
        owner_sub: str,
        thread_data: Optional[Dict[str, Any]] = None,
        agent_record_id: str = "",
        agent_name: str = "",
        basic_chat_model_id: Optional[str] = None,
    ) -> Thread:
        """Create a new thread owned by `owner_sub`.

        The agent may be left unset here: an empty thread has no memory events
        yet, so the first turn can still pin it (see get_or_create_thread).
        """
        thread_id = str(uuid.uuid4())

        thread = Thread(
            thread_id=thread_id,
            created_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
            values=thread_data or {},
            status=ThreadStatusEnum.IDLE,
            metadata=None,
            owner_sub=owner_sub,
            agent_record_id=agent_record_id,
            agent_name=agent_name,
            basic_chat_model_id=self._basic_model(agent_record_id, basic_chat_model_id),
        )
        return self.repository.create(thread)

    @staticmethod
    def _basic_model(agent_record_id: str, model_id: Optional[str]) -> Optional[str]:
        """The model to pin, which only a basic-chat thread has."""
        return model_id if agent_record_id == BASIC_CHAT_RECORD_ID else None

    @staticmethod
    def _has_turns(thread: Thread) -> bool:
        """Whether anything has been said in this thread.

        The dividing line for pinning an unpinned thread: the hazard is only ever
        about memory events that already exist, and a thread with no turns has
        none.
        """
        messages = (thread.values or {}).get("messages")
        return bool(messages)

    def get_or_create_thread(
        self,
        thread_id: str,
        owner_sub: str,
        initial_values: Optional[Dict[str, Any]] = None,
        agent_record_id: str = "",
        agent_name: str = "",
        basic_chat_model_id: Optional[str] = None,
        agent_target: Optional[Dict[str, Optional[str]]] = None,
    ) -> Thread:
        """Get thread or create if it doesn't exist.

        The streaming entry point, and the reason ownership cannot be a
        read-path concern: the client picks the thread id, so without the check
        below a caller could stream into someone else's thread and have their
        messages appended to that conversation.

        Deliberately no admin override, unlike require_owned. Admin may read and
        delete anyone's thread, but *sending a turn* is not administration: the
        run binds AgentCore Memory to the caller's own actor id, so an admin
        streaming into another user's thread would write that conversation into
        its own memory scope and pull its own recall into the reply — corrupting
        both sides rather than inspecting one. Admins talk in their own threads;
        `POST /threads/{id}/runs/stream` on someone else's is a 403 for them too.

        Also where the thread's agent is pinned and enforced. That check has to
        live here rather than in the client for the same reason ownership does:
        the request names both the thread and the agent, so nothing but the
        server can refuse the combination.
        """
        thread = self.repository.get(thread_id)
        if not thread:
            thread = Thread(
                thread_id=thread_id,
                created_at=datetime.utcnow().isoformat(),
                updated_at=datetime.utcnow().isoformat(),
                values=initial_values or {},
                status=ThreadStatusEnum.BUSY,
                metadata=None,
                owner_sub=owner_sub,
                agent_record_id=agent_record_id,
                agent_name=agent_name,
                basic_chat_model_id=self._basic_model(agent_record_id, basic_chat_model_id),
                agent_target=agent_target if agent_record_id else None,
            )
            thread = self.repository.create(thread)
        else:
            if not thread.owner_sub or thread.owner_sub != owner_sub:
                raise ThreadForbidden(
                    f"Thread {thread_id} belongs to another user"
                )
            thread = self._pin_agent(
                thread, agent_record_id, agent_name, basic_chat_model_id
            )
            # Remember the target the registry answered with, so the next turn
            # with the same ARNs need not ask again (services/agent_access.py).
            if agent_target and agent_record_id and thread.agent_record_id == agent_record_id:
                thread.agent_target = agent_target
            # Set status to busy when starting a new stream execution
            thread.status = ThreadStatusEnum.BUSY
            thread = self.repository.update(thread_id, thread)
        return thread

    def _pin_agent(
        self,
        thread: Thread,
        agent_record_id: str,
        agent_name: str,
        basic_chat_model_id: Optional[str] = None,
    ) -> Thread:
        """Bind the thread to this agent, or refuse if it is bound to another.

        Three cases, in the order they are decided:

        - Already pinned to this agent: nothing to do, the common path.
        - Pinned to a different one: refuse. Continuing would either mix both
          agents' turns into one AgentCore Memory event stream or hand the agent
          a conversation it has no memory of — see Thread.agent_record_id.
        - Not pinned: adopt this agent, but only while the thread has no turns.
          A thread with turns predates pinning, and its memory events are already
          attributed to an agent this record cannot name. Claiming it for whoever
          happens to speak next would assert a fact we do not have, so those stay
          unpinned and refuse — the backfill script is how they get an agent.

        A caller that names no agent (`agent_record_id` empty) is left alone
        rather than refused: a local runtime configured by ARN alone has no
        registry record, and that path worked before pinning existed.

        A basic-chat thread additionally pins its model: the same runtime under
        a different model is, for the conversation's memory, a different agent.
        One written before the model was recorded cannot be continued at all.
        """
        if not agent_record_id:
            return thread

        if thread.agent_record_id:
            if thread.agent_record_id != agent_record_id:
                raise ThreadAgentMismatch(
                    f"Thread {thread.thread_id} is bound to agent "
                    f"'{thread.agent_name or thread.agent_record_id}'. Start a "
                    "new chat to talk to a different agent."
                )
            if agent_record_id == BASIC_CHAT_RECORD_ID:
                if not thread.basic_chat_model_id:
                    raise ThreadAgentMismatch(
                        f"Thread {thread.thread_id} has no recorded basic-chat "
                        "model and cannot be continued. Start a new chat."
                    )
                if basic_chat_model_id != thread.basic_chat_model_id:
                    raise ThreadAgentMismatch(
                        f"Thread {thread.thread_id} is pinned to model "
                        f"'{thread.basic_chat_model_id}'. Start a new chat to "
                        "use a different model."
                    )
            # Refresh the display name: the record may have been renamed since.
            if agent_name and thread.agent_name != agent_name:
                thread.agent_name = agent_name
            return thread

        if self._has_turns(thread):
            raise ThreadAgentMismatch(
                f"Thread {thread.thread_id} predates agent pinning and cannot "
                "be continued: which agent wrote its history is not recorded. "
                "Start a new chat."
            )

        thread.agent_record_id = agent_record_id
        thread.agent_name = agent_name
        thread.basic_chat_model_id = self._basic_model(agent_record_id, basic_chat_model_id)
        return thread

    def update_thread_state(
        self,
        thread_id: str,
        update: ThreadStateUpdate,
    ) -> Thread:
        """Update thread state"""
        thread = self.repository.get(thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found")

        # Merge values
        if update.values:
            if thread.values is None:
                thread.values = {}
            thread.values.update(update.values)
        
        # Merge metadata if provided
        if update.metadata:
            if thread.metadata is None:
                thread.metadata = {}
            thread.metadata.update(update.metadata)

        thread.updated_at = datetime.utcnow().isoformat()
        return self.repository.update(thread_id, thread)

    def update_thread_status(
        self,
        thread_id: str,
        status: ThreadStatusEnum,
    ) -> Thread:
        """Update thread status"""
        thread = self.repository.get(thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found")
        
        thread.status = status
        thread.updated_at = datetime.utcnow().isoformat()
        return self.repository.update(thread_id, thread)

    def update_thread_messages(
        self,
        thread_id: str,
        messages: List[Dict[str, Any]],
    ) -> Thread:
        """Update thread messages - saves both human and AI messages"""
        thread = self.repository.get(thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found")
        
        if thread.values is None:
            thread.values = {}
        if "messages" not in thread.values:
            thread.values["messages"] = []
        
        existing_messages = thread.values["messages"]
        
        # Update messages: replace messages with same ID and type, or add new
        # Preserve order by finding and replacing in place, or appending at the end
        for new_msg in messages:
            if isinstance(new_msg, dict) and new_msg.get("id") and new_msg.get("type"):
                msg_id = new_msg.get("id")
                msg_type = new_msg.get("type")
                
                # Find and replace existing message with same ID and type
                found = False
                for i, existing_msg in enumerate(existing_messages):
                    if isinstance(existing_msg, dict) and existing_msg.get("id") == msg_id and existing_msg.get("type") == msg_type:
                        existing_messages[i] = new_msg
                        found = True
                        break
                
                # If not found, append new message (maintains order)
                if not found:
                    existing_messages.append(new_msg)
            else:
                # Messages without ID or type are always added (for backward compatibility)
                existing_messages.append(new_msg)
        
        thread.values["messages"] = existing_messages
        thread.updated_at = datetime.utcnow().isoformat()
        return self.repository.update(thread_id, thread)

    def merge_thread_values(
        self,
        thread_id: str,
        new_values: Dict[str, Any],
    ) -> Thread:
        """Merge new values into thread, preserving existing messages"""
        thread = self.repository.get(thread_id)
        if not thread:
            raise ValueError(f"Thread {thread_id} not found")
        
        if thread.values is None:
            thread.values = {}
        
        # Preserve existing messages
        existing_messages = thread.values.get("messages", [])
        
        # Add new messages from request
        if "messages" in new_values:
            new_messages = new_values.get("messages", [])
            
            # Create a map of existing message IDs to avoid duplicates
            existing_ids = {msg.get("id") for msg in existing_messages if isinstance(msg, dict) and msg.get("id")}
            
            # Add new messages that don't already exist
            for new_msg in new_messages:
                if isinstance(new_msg, dict) and new_msg.get("id"):
                    if new_msg.get("id") not in existing_ids:
                        existing_messages.append(new_msg)
                        existing_ids.add(new_msg.get("id"))
                else:
                    # Messages without IDs are always added
                    existing_messages.append(new_msg)
            
            # Keep existing messages and add new ones
            thread.values["messages"] = existing_messages
            # Update other values (todos, files, etc.) normally
            other_values = {k: v for k, v in new_values.items() if k != "messages"}
            thread.values.update(other_values)
        else:
            # No messages in request, just update other values
            thread.values.update(new_values)
        
        thread.updated_at = datetime.utcnow().isoformat()
        return self.repository.update(thread_id, thread)

    def delete_thread(self, thread_id: str) -> None:
        """Delete a thread"""
        if not self.repository.get(thread_id):
            raise ValueError(f"Thread {thread_id} not found")
        self.repository.delete(thread_id)







