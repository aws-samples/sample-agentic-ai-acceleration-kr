"""AgentCore Memory hook.

Restores a session's recent turns when the agent initializes and records new
turns as they are added. Memory is supplementary: every failure here is logged
and swallowed, because losing recall is a degraded answer while raising would
lose the user's turn outright.

Events are scoped by (memory_id, actor_id, session_id) and nothing here narrows
that further — in particular the scope does not name the agent, and runtimes
deployed from this repo share one MEMORY_ID. The session id comes from the
thread id (server/agents/agentcore_client.py::_session_id), so two agents
answering in one thread would read each other's turns back as their own. That is
prevented on the server, by pinning a thread to one agent: see
server/models/thread.py::Thread.agent_record_id.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from bedrock_agentcore.memory import MemoryClient
from strands.hooks.events import AgentInitializedEvent, MessageAddedEvent
from strands.hooks.registry import HookProvider, HookRegistry

logger = logging.getLogger(__name__)

# How many prior turns to restore. The harness default for its own truncation is
# a sliding window; this mirrors that intent without unbounded context growth.
RECENT_TURNS = 10

# How many session-summary chunks to pull. A session's summary is a handful of
# chunks; 5 covers a long chat without bloating the prompt.
SUMMARY_TOP_K = 5


class MemoryHook(HookProvider):
    def __init__(
        self,
        memory_client: MemoryClient,
        memory_id: str,
        actor_id: str,
        session_id: str,
        long_term_recall: bool = False,
        skip_recall: bool = False,
    ):
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.actor_id = actor_id
        self.session_id = session_id
        # When on, prepend a session summary of turns older than the recent
        # window. Requires a SUMMARIZATION strategy on this memory; without one
        # retrieve_memories returns nothing and this is a no-op.
        self.long_term_recall = long_term_recall
        # Set on a thread's first turn, where the session holds no prior events.
        # Recall would then round-trip to Memory twice only to restore nothing —
        # ~0.7-1.1s of measured latency in front of the first model token, on the
        # exact "starting" moment the user waits through. The server decides this
        # (it knows the thread's history); the hook only obeys. Recording still
        # runs on_message_added, so this turn is saved for the next one to recall.
        self.skip_recall = skip_recall

    @staticmethod
    def _text_of(message: Dict[str, Any]) -> Optional[str]:
        """First text block of a message, or None if it carries no text.

        Tool results and images have no text; storing them would write empty
        turns into memory.
        """
        content = message.get("content") or []
        if not isinstance(content, list):
            return None
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                return block["text"]
        return None

    def _summary_namespace(self) -> str:
        # Must match the namespaceTemplates set on the SUMMARIZATION strategy
        # (see enable_longterm_memory.py / installer). Session-scoped so recall
        # never crosses chats.
        return f"/summaries/{self.actor_id}/{self.session_id}"

    @staticmethod
    def _record_text(record: Dict[str, Any]) -> Optional[str]:
        if not isinstance(record, dict):
            return None
        content = record.get("content")
        if isinstance(content, dict) and content.get("text"):
            return content["text"]
        if record.get("text"):
            return record["text"]
        return None

    def _latest_user_text(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("text"):
                    return block["text"]
        return None

    def _session_summary(self, query: str) -> Optional[str]:
        try:
            records = self.memory_client.retrieve_memories(
                memory_id=self.memory_id,
                namespace=self._summary_namespace(),
                query=query,
                top_k=SUMMARY_TOP_K,
            )
        except Exception as exc:
            logger.warning("Could not retrieve summary for %s: %s", self.session_id, exc)
            return None
        chunks = [t for t in (self._record_text(r) for r in records or []) if t]
        return "\n".join(chunks) if chunks else None

    def _recent_turns(self) -> List[Any]:
        try:
            return (
                self.memory_client.get_last_k_turns(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    k=RECENT_TURNS,
                )
                or []
            )
        except Exception as exc:
            logger.warning("Could not load memory for %s: %s", self.session_id, exc)
            return []

    def on_agent_initialized(self, event: AgentInitializedEvent):
        """Prepend this session's recent turns to the agent's messages."""
        if self.skip_recall:
            return

        # The recent-turns read and the summary read are independent and both sit
        # in front of the first model token, so fire them together rather than in
        # series — the wait is the slower of the two, not their sum. Each still
        # fails soft on its own (see _recent_turns / _session_summary).
        query = None
        if self.long_term_recall:
            query = self._latest_user_text(event.agent.messages) or (
                "summary of the conversation so far"
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            turns_future = pool.submit(self._recent_turns)
            summary_future = (
                pool.submit(self._session_summary, query) if query is not None else None
            )
            recent_turns = turns_future.result()
            summary = summary_future.result() if summary_future is not None else None

        history: List[Dict[str, Any]] = []
        for turn in recent_turns:
            for message in turn:
                text = (message.get("content") or {}).get("text")
                if not text:
                    continue
                role = "assistant" if message.get("role") == "ASSISTANT" else "user"
                history.append({"role": role, "content": [{"text": text}]})

        preamble: List[Dict[str, Any]] = []
        if summary:
            preamble = [
                {"role": "user", "content": [{"text": f"[이전 대화 요약]\n{summary}"}]}
            ]

        if not history and not preamble:
            return

        # Prepend, never assign: the agent already holds the in-flight turn, and
        # replacing the list would drop the message being answered. Order ends up
        # [summary] -> [recent turns] -> [in-flight].
        event.agent.messages[:0] = preamble + history

    def on_message_added(self, event: MessageAddedEvent):
        """Record the newest message as a memory event."""
        messages = event.agent.messages
        if not messages:
            return

        latest = messages[-1]
        role = latest.get("role")
        if role not in ("user", "assistant"):
            return

        text = self._text_of(latest)
        if not text:
            return

        try:
            self.memory_client.save_conversation(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(text, role)],
            )
        except Exception as exc:
            # Never raise: a memory outage must not fail the conversation.
            logger.warning("Could not save memory for %s: %s", self.session_id, exc)

    def register_hooks(self, registry: HookRegistry):
        registry.add_callback(MessageAddedEvent, self.on_message_added)
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)
