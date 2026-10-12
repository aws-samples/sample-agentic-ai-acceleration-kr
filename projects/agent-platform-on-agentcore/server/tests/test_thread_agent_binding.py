"""A thread belongs to one registry agent, and no turn from another gets in.

Ownership (test_thread_ownership.py) asks *who* may speak in a thread. This asks
*which agent* may answer, which is a separate axis with a separate failure mode:
both callers here are the legitimate owner.

The hazard is AgentCore Memory. Events are scoped by (memory_id, actor_id,
session_id) and session_id is derived from the thread id alone
(AgentCoreClient._session_id) — the agent is not in the scope. So two agents in
one thread either write into a shared event stream (runtime agents deployed from
this repo share one MEMORY_ID, so agent B reads A's turns back as its own
`assistant` messages) or read nothing at all (a harness owns its own managed
memory, so B sees an empty history while the UI still renders A's transcript from
DynamoDB). Neither surfaces as an error, which is why it is pinned here.

The storage stub goes through the real ThreadRepository item-building code for the
same reason test_thread_ownership.py does: both writes are whole-item put_item
calls, so an attribute missing from the dict is *erased*. For agent_record_id that
would silently unpin the thread rather than 403, which is quieter and worse.
"""
import json
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.threads as thread_routes  # noqa: E402
from models.common import ThreadStatus  # noqa: E402
from models.thread import Thread  # noqa: E402
from repositories.thread_repository import ThreadRepository  # noqa: E402
from services.thread_service import (  # noqa: E402
    ThreadAgentMismatch,
    ThreadService,
)

OWNER = auth.AuthUser(sub="sub-owner", username="owner", groups=["user"])

AGENT_A = ("rec-a", "Research Agent")
AGENT_B = ("rec-b", "Coding Agent")

PINNED = "t-pinned"          # bound to AGENT_A, has a turn
LEGACY_WITH_TURNS = "t-old"  # no agent, has a turn — cannot be continued
LEGACY_EMPTY = "t-empty"     # no agent, no turns — adoptable


def _thread(thread_id, *, agent=None, messages=None):
    record_id, name = agent if agent else ("", "")
    return Thread(
        thread_id=thread_id,
        created_at="2026-08-01T00:00:00",
        updated_at="2026-08-01T00:00:00",
        values={"messages": messages if messages is not None else []},
        metadata={},
        owner_sub=OWNER.sub,
        agent_record_id=record_id,
        agent_name=name,
    )


TURN = [{"id": "m1", "type": "human", "content": "hi"}]


class _CapturingTable:
    def __init__(self):
        self.item = None

    def put_item(self, Item):  # noqa: N803 — boto3's parameter name
        self.item = Item


def _real_item(method, thread_id, thread):
    """The Item the production repository method writes, captured not sent."""
    repository = ThreadRepository.__new__(ThreadRepository)
    repository.table = _CapturingTable()
    if method == "create":
        ThreadRepository.create(repository, thread)
    else:
        ThreadRepository.update(repository, thread_id, thread)
    return repository.table.item


class StubRepository:
    """In-memory stand-in that serialises through the real repository code."""

    def __init__(self):
        self.items = {
            PINNED: _real_item(
                "create", PINNED, _thread(PINNED, agent=AGENT_A, messages=TURN)
            ),
            LEGACY_WITH_TURNS: _real_item(
                "create", LEGACY_WITH_TURNS, _thread(LEGACY_WITH_TURNS, messages=TURN)
            ),
            LEGACY_EMPTY: _real_item(
                "create", LEGACY_EMPTY, _thread(LEGACY_EMPTY)
            ),
        }

    def get(self, thread_id):
        item = self.items.get(thread_id)
        return ThreadRepository._to_thread(item) if item else None

    def create(self, thread):
        self.items[thread.thread_id] = _real_item("create", thread.thread_id, thread)
        return thread

    def update(self, thread_id, thread):
        self.items[thread_id] = _real_item("update", thread_id, thread)
        return thread

    def delete(self, thread_id):
        self.items.pop(thread_id, None)

    def agent_of(self, thread_id):
        """Read the stored attribute, not a model field: an erased attribute must
        read as absent rather than as the Thread default."""
        return self.items[thread_id].get("agent_record_id")

    def name_of(self, thread_id):
        return self.items[thread_id].get("agent_name")

    def search(self, owner_sub, **kwargs):
        return [
            ThreadRepository._to_thread(i)
            for i in self.items.values()
            if owner_sub is None or i.get("owner_sub") == owner_sub
        ]


class StubStreamingService:
    """Runs the real pinning decision, without AgentCore.

    Mirrors the production call: the agent comes off request.config, exactly as
    StreamingService.stream_thread_execution reads it.
    """

    def __init__(self, thread_service):
        self.thread_service = thread_service
        self.calls = []

    async def stream_thread_execution(
        self, thread_id, request, actor_id=None, owner_sub="", caller=None
    ):
        config = request.config
        self.thread_service.get_or_create_thread(
            thread_id=thread_id,
            owner_sub=owner_sub,
            initial_values=None,
            agent_record_id=getattr(config, "registry_record_id", None) or "",
            agent_name=getattr(config, "registry_agent_name", None) or "",
        )
        self.calls.append((thread_id, owner_sub))
        return {"ok": True}


@pytest.fixture
def repo():
    return StubRepository()


@pytest.fixture
def service(repo):
    return ThreadService(repo)


@pytest.fixture
def app(monkeypatch, service):
    monkeypatch.setattr(thread_routes, "thread_service", service)
    monkeypatch.setattr(
        thread_routes, "streaming_service", StubStreamingService(service)
    )
    application = FastAPI()
    application.include_router(thread_routes.router)
    application.dependency_overrides[auth.current_user] = lambda: OWNER
    return application


def _stream(app, thread_id, agent):
    """POST a turn addressed to `agent`, as the web client does."""
    record_id, name = agent if agent else (None, None)
    config = {}
    if record_id:
        config["registry_record_id"] = record_id
        config["registry_agent_name"] = name
    return TestClient(app, raise_server_exceptions=False).post(
        f"/threads/{thread_id}/runs/stream", json={"config": config}
    )


# --- serialisation: the field has to survive both whole-item writes ----------


def test_create_persists_the_agent_attributes():
    item = _real_item("create", "t-new", _thread("t-new", agent=AGENT_A))

    assert item["agent_record_id"] == AGENT_A[0]
    assert item["agent_name"] == AGENT_A[1]


def test_update_persists_the_agent_attributes():
    """update() is a whole-item put_item: an attribute absent from the dict it
    builds is erased, which would unpin the thread and let any agent in."""
    item = _real_item("update", PINNED, _thread(PINNED, agent=AGENT_A))

    assert item["agent_record_id"] == AGENT_A[0]
    assert item["agent_name"] == AGENT_A[1]


def test_a_state_patch_does_not_unpin_the_thread(app, repo):
    """The round trip: PATCH /state goes through update()."""
    response = TestClient(app).patch(
        f"/threads/{PINNED}/state", json={"values": {"a": 1}}
    )

    assert response.status_code == 200
    assert repo.agent_of(PINNED) == AGENT_A[0]


def test_a_metadata_patch_cannot_repoint_the_thread(app, repo):
    """PATCH /state merges caller-supplied metadata, which is why the agent is a
    top-level field and not a key inside it."""
    response = TestClient(app).patch(
        f"/threads/{PINNED}/state",
        json={"metadata": {"agent_record_id": AGENT_B[0]}},
    )

    assert response.status_code == 200
    assert repo.agent_of(PINNED) == AGENT_A[0]


def test_a_status_write_keeps_the_agent(app, repo):
    """update_thread_status shares the same put_item, and the streaming path
    calls it on every terminal state."""
    thread_routes.thread_service.update_thread_status(PINNED, ThreadStatus.IDLE)

    assert repo.agent_of(PINNED) == AGENT_A[0]


# --- pinning and enforcement -------------------------------------------------


def test_a_new_thread_is_pinned_to_the_agent_that_starts_it(app, repo):
    assert _stream(app, "t-fresh", AGENT_A).status_code == 200

    assert repo.agent_of("t-fresh") == AGENT_A[0]
    assert repo.name_of("t-fresh") == AGENT_A[1]


def test_the_same_agent_may_continue(app):
    assert _stream(app, PINNED, AGENT_A).status_code == 200


def test_a_different_agent_is_409(app, repo):
    """The conflict this whole feature exists for. 409 and not 403: the caller
    owns the thread, so it is a state conflict, not an entitlement one."""
    response = _stream(app, PINNED, AGENT_B)

    assert response.status_code == 409, response.text
    # And the thread is still bound to the original agent.
    assert repo.agent_of(PINNED) == AGENT_A[0]


def test_the_mismatch_message_names_the_bound_agent(app):
    """The client turns this into "start a new chat", so the detail has to say
    which agent already owns the thread rather than just refusing."""
    detail = _stream(app, PINNED, AGENT_B).json()["detail"]

    assert AGENT_A[1] in detail


def test_a_rename_refreshes_the_label_without_breaking_the_pin(app, repo):
    """Only the record id decides; the name is a label that may drift."""
    response = _stream(app, PINNED, (AGENT_A[0], "Renamed Agent"))

    assert response.status_code == 200
    assert repo.agent_of(PINNED) == AGENT_A[0]
    assert repo.name_of(PINNED) == "Renamed Agent"


# --- threads from before pinning existed -------------------------------------


def test_an_empty_legacy_thread_is_adopted(app, repo):
    """No turns means no memory events, so there is nothing to collide with."""
    assert _stream(app, LEGACY_EMPTY, AGENT_A).status_code == 200

    assert repo.agent_of(LEGACY_EMPTY) == AGENT_A[0]


def test_a_legacy_thread_with_turns_cannot_be_continued(app, repo):
    """Its memory events belong to an agent this record cannot name, so adopting
    it for whoever speaks next would assert a fact we do not have."""
    response = _stream(app, LEGACY_WITH_TURNS, AGENT_A)

    assert response.status_code == 409, response.text
    assert repo.agent_of(LEGACY_WITH_TURNS) == ""


def test_backfilling_makes_a_legacy_thread_continuable(app, repo, service):
    """What scripts/backfill_thread_agent.py buys: the same 409 above turns into
    a 200 once the thread names an agent."""
    thread = repo.get(LEGACY_WITH_TURNS)
    thread.agent_record_id, thread.agent_name = AGENT_A
    repo.update(LEGACY_WITH_TURNS, thread)

    assert _stream(app, LEGACY_WITH_TURNS, AGENT_A).status_code == 200
    assert _stream(app, LEGACY_WITH_TURNS, AGENT_B).status_code == 409


# --- the unnamed-agent path --------------------------------------------------


def test_a_turn_naming_no_agent_is_left_alone(app, repo):
    """A runtime configured by ARN alone has no registry record, and that path
    worked before pinning existed — it must not start failing now."""
    assert _stream(app, PINNED, None).status_code == 200

    assert repo.agent_of(PINNED) == AGENT_A[0]


def test_a_thread_started_without_an_agent_stays_adoptable(service, repo):
    """Pinning is skipped, not recorded as "no agent", so a later turn that does
    name one can still claim the thread while it has no turns."""
    service.get_or_create_thread(
        thread_id="t-anon", owner_sub=OWNER.sub, initial_values=None
    )
    assert repo.agent_of("t-anon") == ""

    service.get_or_create_thread(
        thread_id="t-anon",
        owner_sub=OWNER.sub,
        initial_values=None,
        agent_record_id=AGENT_A[0],
        agent_name=AGENT_A[1],
    )

    assert repo.agent_of("t-anon") == AGENT_A[0]


# --- the service-level contract ---------------------------------------------


def test_the_service_raises_rather_than_returning_a_thread(service):
    """The route maps this to 409; nothing downstream should have to re-check."""
    with pytest.raises(ThreadAgentMismatch):
        service.get_or_create_thread(
            thread_id=PINNED,
            owner_sub=OWNER.sub,
            initial_values=None,
            agent_record_id=AGENT_B[0],
            agent_name=AGENT_B[1],
        )


def test_a_refused_turn_leaves_the_thread_idle(service, repo):
    """The mismatch is decided before the status flips to busy, so a refused turn
    must not leave the thread stuck showing a run in progress."""
    before = repo.get(PINNED).status

    with pytest.raises(ThreadAgentMismatch):
        service.get_or_create_thread(
            thread_id=PINNED,
            owner_sub=OWNER.sub,
            initial_values=None,
            agent_record_id=AGENT_B[0],
        )

    assert repo.get(PINNED).status == before
