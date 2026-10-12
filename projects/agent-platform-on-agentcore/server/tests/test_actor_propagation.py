"""
What each agent client sends to AWS.

Two things the harness contract depends on: only the newest user message goes
over the wire (the harness reloads the rest from Memory), and the caller's
identity rides along as actorId so one user's memory is not another's.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.harness_client import HarnessClient  # noqa: E402
from agents.agentcore_client import AgentCoreClient  # noqa: E402
from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"

VALUES = {
    "messages": [
        {"id": "1", "type": "human", "content": "first question"},
        {"id": "2", "type": "ai", "content": "first answer"},
        {"id": "3", "type": "human", "content": "second question"},
    ]
}


class StubAgentCore:
    def __init__(self):
        self.params = None

    def invoke_harness(self, **params):
        self.params = params
        return {"stream": []}


def invoke(values=VALUES, actor_id="user-sub-1"):
    client = HarnessClient(harness_arn=HARNESS_ARN, region_name="us-east-1")
    stub = StubAgentCore()
    client.agentcore_client = stub

    async def drain():
        async for _ in client.execute_stream(
            thread_id="t-1", values=values, config={}, actor_id=actor_id
        ):
            pass

    asyncio.run(drain())
    return stub.params


def test_only_the_latest_user_message_is_sent():
    """The harness reloads prior turns from Memory; resending them duplicates."""
    params = invoke()

    assert params["messages"] == [
        {"role": "user", "content": [{"text": "second question"}]}
    ]


def test_actor_id_is_passed_for_per_user_memory_isolation():
    assert invoke(actor_id="user-sub-2")["actorId"] == "user-sub-2"


def test_actor_id_is_omitted_when_unknown():
    """An empty actorId would be rejected; absent means unscoped."""
    assert "actorId" not in invoke(actor_id=None)


RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"


class StubRuntime:
    def __init__(self):
        self.params = None

    def invoke_agent_runtime(self, **params):
        self.params = params
        return {"contentType": "application/json", "response": None}


def runtime_payload(values=VALUES, actor_id="user-sub-1"):
    client = AgentCoreClient(agent_runtime_arn=RUNTIME_ARN, region_name="us-east-1")
    stub = StubRuntime()
    client.agentcore_client = stub

    async def drain():
        async for _ in client.execute_stream(
            thread_id="t-1", values=values, config={}, actor_id=actor_id
        ):
            pass

    asyncio.run(drain())
    return json.loads(stub.params["payload"])


def test_conversation_history_is_not_sent():
    """The runtime never read this key; sending it implied history it ignored."""
    assert "conversation_history" not in runtime_payload()


def test_runtime_payload_carries_the_prompt_and_actor():
    payload = runtime_payload()

    assert payload["prompt"] == "second question"
    assert payload["actor_id"] == "user-sub-1"


class StubThreadService:
    """Minimal thread service: enough for the streaming path to run."""

    class _Repo:
        def update(self, thread_id, thread):
            return thread

    def __init__(self):
        self.repository = self._Repo()
        self.statuses = []

    def get_or_create_thread(
        self,
        thread_id,
        owner_sub="",
        initial_values=None,
        agent_record_id="",
        agent_name="", **_kwargs):
        return type("T", (), {"values": dict(VALUES), "updated_at": ""})()

    def get_thread(self, thread_id):
        return type("T", (), {"values": dict(VALUES), "updated_at": ""})()

    def update_thread_status(self, thread_id, status):
        self.statuses.append(status)


class RecordingClient:
    def __init__(self):
        self.actor_id = "unset"

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        self.actor_id = actor_id
        return
        yield  # pragma: no cover — makes this an async generator


def test_actor_reaches_the_agent_client():
    threads = StubThreadService()
    client = RecordingClient()
    service = StreamingService(thread_service=threads, agentcore_client=client)
    service._get_agent_client = lambda config=None: client

    response = service.stream_thread_execution(
        "t-1", StreamRequest(values=VALUES), actor_id="user-sub-9"
    )

    async def drain():
        async for _ in (await response).body_iterator:
            pass

    asyncio.run(drain())
    assert client.actor_id == "user-sub-9"
