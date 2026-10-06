"""Only an APPROVED registry record may be pinned to a chat thread.

The web UI already hides unapproved agents from the picker, but the stream
request names the record id directly, so the picker is advisory — anyone can
POST a turn addressed to a DRAFT or REJECTED record. This gate is the server
half: binding a thread to an agent requires the record to be APPROVED.

Deliberately checked only at binding time. A record that loses approval later
does not brick the threads it already owns — the agent pin (409, see
test_thread_agent_binding.py) already keeps other agents out of them, and
re-checking every turn would cut off conversations mid-history and add a
registry round-trip to each one.

The unnamed-agent path (a runtime addressed by ARN alone, no registry record)
must stay open: it predates both pinning and approval, and a server with no
registry configured has nothing to check against.
"""
import asyncio
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.threads as thread_routes  # noqa: E402
from models.common import StreamRequest  # noqa: E402
from models.registry import RegistryRecordDetail  # noqa: E402
from models.thread import Thread  # noqa: E402
from services.registry_service import RegistryNotConfigured  # noqa: E402
from services.streaming_service import (  # noqa: E402
    AgentNotApproved,
    StreamingService,
)
from services.thread_service import ThreadService  # noqa: E402

OWNER = auth.AuthUser(sub="sub-owner", username="owner", groups=["user"])

APPROVED = ("rec-approved", "Approved Agent")
DRAFT = ("rec-draft", "Draft Agent")
REJECTED = ("rec-rejected", "Rejected Agent")
DELETED = ("rec-gone", "Deleted Agent")

RECORD_STATUS = {
    APPROVED[0]: "APPROVED",
    DRAFT[0]: "DRAFT",
    REJECTED[0]: "REJECTED",
}

PINNED_TO_DRAFT = "t-pinned-draft"  # bound to DRAFT before the gate existed


RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"


class StubRegistry:
    def __init__(self, configured=True):
        self.configured = configured
        self.lookups = []

    def get_record(self, record_id):
        self.lookups.append(record_id)
        if not self.configured:
            raise RegistryNotConfigured("AGENT_REGISTRY_ID is not configured")
        status = RECORD_STATUS.get(record_id)
        if status is None:
            raise ValueError(f"Record {record_id} not found")
        return RegistryRecordDetail(
            record_id=record_id, name=f"agent {record_id}", status=status,
            agent_runtime_arn=RUNTIME_ARN,
        )


class StubRepository:
    """In-memory threads; serialisation fidelity is test_thread_agent_binding's
    concern, not this file's."""

    def __init__(self):
        self.threads = {
            PINNED_TO_DRAFT: Thread(
                thread_id=PINNED_TO_DRAFT,
                created_at="2026-08-01T00:00:00",
                updated_at="2026-08-01T00:00:00",
                values={"messages": [{"id": "m1", "type": "human", "content": "hi"}]},
                metadata={},
                owner_sub=OWNER.sub,
                agent_record_id=DRAFT[0],
                agent_name=DRAFT[1],
                # Bound under the current code: the target the registry answered
                # with at pin time is remembered, so a later turn that sends
                # nothing new has no reason to ask the registry at all.
                agent_target={"agent_runtime_arn": RUNTIME_ARN, "harness_arn": None, "qualifier": None},
            )
        }

    def get(self, thread_id):
        return self.threads.get(thread_id)

    def create(self, thread):
        self.threads[thread.thread_id] = thread
        return thread

    def update(self, thread_id, thread):
        self.threads[thread_id] = thread
        return thread


class StubAgentClient:
    """One clean turn, so a permitted stream can complete."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-ai"}}}
        yield {"event": {"contentBlockDelta": {"delta": {"text": "hello"}}}}
        yield {"event": {"messageStop": {"messageId": "m-ai"}}}


def make_service(registry):
    service = StreamingService(
        thread_service=ThreadService(StubRepository()),
        agentcore_client=StubAgentClient(),
        registry_service=registry,
    )
    service._get_agent_client = lambda config=None: StubAgentClient()
    return service


def make_app(service):
    application = FastAPI()
    application.include_router(thread_routes.router)
    application.dependency_overrides[auth.current_user] = lambda: OWNER
    return application


def _stream(app, service, monkeypatch, thread_id, agent):
    monkeypatch.setattr(thread_routes, "thread_service", service.thread_service)
    monkeypatch.setattr(thread_routes, "streaming_service", service)
    config = {}
    if agent:
        config["registry_record_id"] = agent[0]
        config["registry_agent_name"] = agent[1]
    return TestClient(app, raise_server_exceptions=False).post(
        f"/threads/{thread_id}/runs/stream", json={"config": config}
    )


def _request(agent):
    record_id, name = agent
    return StreamRequest(
        values={"messages": []},
        config={
            "registry_record_id": record_id,
            "registry_agent_name": name,
        },
    )


# --- the service-level contract ----------------------------------------------


@pytest.mark.parametrize("agent", [DRAFT, REJECTED], ids=["draft", "rejected"])
def test_an_unapproved_agent_cannot_start_a_thread(agent):
    service = make_service(StubRegistry())

    with pytest.raises(AgentNotApproved):
        asyncio.run(
            service.stream_thread_execution(
                "t-new", _request(agent), owner_sub=OWNER.sub
            )
        )


def test_the_refusal_names_the_agent_and_its_status():
    """The client shows the detail verbatim, so it has to say which agent and
    why rather than a bare 403."""
    service = make_service(StubRegistry())

    with pytest.raises(AgentNotApproved) as excinfo:
        asyncio.run(
            service.stream_thread_execution(
                "t-new", _request(DRAFT), owner_sub=OWNER.sub
            )
        )

    assert DRAFT[1] in str(excinfo.value)
    assert "DRAFT" in str(excinfo.value)


def test_an_approved_agent_streams(monkeypatch):
    service = make_service(StubRegistry())
    app = make_app(service)

    response = _stream(app, service, monkeypatch, "t-new", APPROVED)

    assert response.status_code == 200, response.text


def test_an_unverifiable_record_fails_closed():
    """A record that cannot be fetched cannot be shown to be approved."""
    service = make_service(StubRegistry())

    with pytest.raises(AgentNotApproved):
        asyncio.run(
            service.stream_thread_execution(
                "t-new", _request(DELETED), owner_sub=OWNER.sub
            )
        )


# --- paths the gate must leave open -------------------------------------------


def test_a_turn_naming_no_agent_skips_the_registry(monkeypatch):
    """A runtime addressed by ARN alone has no record to check."""
    registry = StubRegistry()
    service = make_service(registry)
    app = make_app(service)

    response = _stream(app, service, monkeypatch, "t-anon", None)

    assert response.status_code == 200, response.text
    assert registry.lookups == []


def test_an_unconfigured_registry_does_not_refuse(monkeypatch):
    """No registry means records cannot exist; refusing here would take down
    every locally configured runtime."""
    service = make_service(StubRegistry(configured=False))
    app = make_app(service)

    response = _stream(app, service, monkeypatch, "t-new", APPROVED)

    assert response.status_code == 200, response.text


def test_a_thread_already_pinned_to_the_agent_is_not_rechecked(monkeypatch):
    """Approval is a gate on binding, not a per-turn subscription: a record that
    later loses approval must not brick the conversations it already owns."""
    registry = StubRegistry()
    service = make_service(registry)
    app = make_app(service)

    response = _stream(app, service, monkeypatch, PINNED_TO_DRAFT, DRAFT)

    assert response.status_code == 200, response.text
    assert registry.lookups == []


def test_a_legacy_pin_without_a_remembered_target_reads_the_record_once_for_its_arns(monkeypatch):
    """The one lookup here is for the execution target (services/agent_access.py),
    not a re-check of approval: the DRAFT status it returns does not refuse the
    turn, and the answer is remembered so the next turn asks nothing."""
    registry = StubRegistry()
    service = make_service(registry)
    repo = service.thread_service.repository
    repo.threads[PINNED_TO_DRAFT].agent_target = None
    app = make_app(service)

    response = _stream(app, service, monkeypatch, PINNED_TO_DRAFT, DRAFT)
    assert response.status_code == 200, response.text
    assert registry.lookups == [DRAFT[0]]
    assert repo.threads[PINNED_TO_DRAFT].agent_target is not None

    response = _stream(app, service, monkeypatch, PINNED_TO_DRAFT, DRAFT)
    assert response.status_code == 200, response.text
    assert registry.lookups == [DRAFT[0]]


# --- the route mapping ---------------------------------------------------------


def test_the_route_answers_403(monkeypatch):
    service = make_service(StubRegistry())
    app = make_app(service)

    response = _stream(app, service, monkeypatch, "t-new", REJECTED)

    assert response.status_code == 403, response.text
    assert REJECTED[1] in response.json()["detail"]


def test_a_refused_turn_creates_no_thread(monkeypatch):
    """The gate fires before get_or_create_thread, so a refused first turn must
    not leave an empty thread behind for the sidebar to show."""
    service = make_service(StubRegistry())
    app = make_app(service)

    _stream(app, service, monkeypatch, "t-refused", DRAFT)

    assert service.thread_service.get_thread("t-refused") is None
