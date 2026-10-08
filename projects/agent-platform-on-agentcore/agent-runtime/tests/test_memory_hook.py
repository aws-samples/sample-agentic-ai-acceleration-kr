"""
MemoryHook behaviour that the conversation depends on.

Memory is supplementary: a hook that raises takes the user's turn down with it,
and a hook that overwrites the agent's messages loses the turn in flight.
"""
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.memory_hook import MemoryHook  # noqa: E402


class FakeAgent:
    def __init__(self, messages=None, system_prompt="base"):
        self.messages = messages if messages is not None else []
        self.system_prompt = system_prompt


class FakeEvent:
    def __init__(self, agent):
        self.agent = agent


class FakeMemoryClient:
    def __init__(self, turns=None, summaries=None, fail=False, retrieve_fail=False):
        self._turns = turns or []
        self._summaries = summaries or []
        self.fail = fail
        self.retrieve_fail = retrieve_fail
        self.saved = []
        self.retrieve_calls = []
        self.turn_calls = []

    def get_last_k_turns(self, **kwargs):
        self.turn_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("memory unavailable")
        return self._turns

    def save_conversation(self, **kwargs):
        if self.fail:
            raise RuntimeError("memory unavailable")
        self.saved.append(kwargs)

    def retrieve_memories(self, **kwargs):
        self.retrieve_calls.append(kwargs)
        if self.retrieve_fail:
            raise RuntimeError("retrieve unavailable")
        return self._summaries


def hook(client, long_term_recall=False, skip_recall=False):
    return MemoryHook(
        memory_client=client,
        memory_id="mem-1",
        actor_id="user-1",
        session_id="sess-1",
        long_term_recall=long_term_recall,
        skip_recall=skip_recall,
    )


def test_history_is_prepended_not_substituted():
    """The in-flight turn must survive loading history."""
    turns = [[{"role": "USER", "content": {"text": "earlier question"}}]]
    agent = FakeAgent(messages=[{"role": "user", "content": [{"text": "current"}]}])

    hook(FakeMemoryClient(turns)).on_agent_initialized(FakeEvent(agent))

    assert agent.messages[-1] == {"role": "user", "content": [{"text": "current"}]}
    assert agent.messages[0]["content"][0]["text"] == "earlier question"


def test_load_failure_leaves_the_agent_usable():
    agent = FakeAgent(messages=[{"role": "user", "content": [{"text": "current"}]}])

    hook(FakeMemoryClient(fail=True)).on_agent_initialized(FakeEvent(agent))

    assert agent.messages == [{"role": "user", "content": [{"text": "current"}]}]


def test_save_failure_does_not_raise():
    """A memory outage must degrade the feature, not fail the turn."""
    agent = FakeAgent(messages=[{"role": "user", "content": [{"text": "hi"}]}])

    # Deliberately assertion-free: the contract under test is that this call
    # returns rather than raising. pytest fails the test if it raises.
    hook(FakeMemoryClient(fail=True)).on_message_added(FakeEvent(agent))


def test_system_prompt_is_left_alone():
    """Appending per-initialization compounds across a warm container's reuse."""
    agent = FakeAgent(messages=[], system_prompt="base")

    hook(FakeMemoryClient()).on_agent_initialized(FakeEvent(agent))

    assert agent.system_prompt == "base"


def test_user_messages_are_saved():
    client = FakeMemoryClient()
    agent = FakeAgent(messages=[{"role": "user", "content": [{"text": "hello"}]}])

    hook(client).on_message_added(FakeEvent(agent))

    assert client.saved[0]["messages"] == [("hello", "user")]


def test_messages_without_text_are_skipped():
    """Tool-result blocks carry no text; saving them would store empty turns."""
    client = FakeMemoryClient()
    agent = FakeAgent(messages=[{"role": "user", "content": [{"toolResult": {}}]}])

    hook(client).on_message_added(FakeEvent(agent))

    assert client.saved == []


def test_session_summary_is_prepended_before_recent_turns():
    """With recall on, the session summary leads, then recent turns, then the in-flight turn."""
    turns = [[{"role": "USER", "content": {"text": "earlier question"}}]]
    summaries = [{"content": {"text": "user was troubleshooting order #123"}}]
    agent = FakeAgent(messages=[{"role": "user", "content": [{"text": "current"}]}])

    hook(FakeMemoryClient(turns, summaries), long_term_recall=True).on_agent_initialized(
        FakeEvent(agent)
    )

    assert "[이전 대화 요약]" in agent.messages[0]["content"][0]["text"]
    assert "order #123" in agent.messages[0]["content"][0]["text"]
    assert agent.messages[1]["content"][0]["text"] == "earlier question"
    assert agent.messages[-1] == {"role": "user", "content": [{"text": "current"}]}


def test_recall_off_never_calls_retrieve():
    """Default off: no summary block, and retrieve_memories is not called."""
    client = FakeMemoryClient(
        turns=[[{"role": "USER", "content": {"text": "earlier"}}]],
        summaries=[{"content": {"text": "should not appear"}}],
    )
    agent = FakeAgent(messages=[{"role": "user", "content": [{"text": "current"}]}])

    hook(client, long_term_recall=False).on_agent_initialized(FakeEvent(agent))

    assert client.retrieve_calls == []
    assert all("should not appear" not in m["content"][0]["text"] for m in agent.messages)


def test_skip_recall_makes_no_memory_reads():
    """A thread's first turn has nothing stored yet, so both recall reads —
    get_last_k_turns and retrieve_memories — are pure latency returning nothing.
    skip_recall must short-circuit before either fires and leave the turn intact.
    """
    client = FakeMemoryClient(
        turns=[[{"role": "USER", "content": {"text": "earlier"}}]],
        summaries=[{"content": {"text": "should not appear"}}],
    )
    agent = FakeAgent(messages=[{"role": "user", "content": [{"text": "current"}]}])

    hook(client, long_term_recall=True, skip_recall=True).on_agent_initialized(
        FakeEvent(agent)
    )

    assert client.turn_calls == []
    assert client.retrieve_calls == []
    assert agent.messages == [{"role": "user", "content": [{"text": "current"}]}]


def test_summary_retrieval_failure_degrades():
    """A retrieval outage must not drop the recent turns or the in-flight turn."""
    client = FakeMemoryClient(
        turns=[[{"role": "USER", "content": {"text": "earlier"}}]],
        retrieve_fail=True,
    )
    agent = FakeAgent(messages=[{"role": "user", "content": [{"text": "current"}]}])

    hook(client, long_term_recall=True).on_agent_initialized(FakeEvent(agent))

    assert agent.messages[0]["content"][0]["text"] == "earlier"
    assert agent.messages[-1] == {"role": "user", "content": [{"text": "current"}]}
