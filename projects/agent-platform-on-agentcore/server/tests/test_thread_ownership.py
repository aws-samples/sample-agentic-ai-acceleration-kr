"""A thread belongs to one Cognito sub, and no route hands it to anyone else.

test_route_auth.py pins *which* auth dependency each route has; this file pins
what happens once the caller is authenticated. The two are separate axes: every
route here is reachable by any logged-in user, and that was exactly the bug —
one session was enough to read and delete another account's conversation.

The stubs are hand-written rather than mock.patch'd so the storage layer stays
honest about the trap that motivated `owner_sub` being carried in
ThreadRepository.update: a fake that ignored the write would hide it.
"""
import json
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.artifacts as artifact_routes  # noqa: E402
import routes.threads as thread_routes  # noqa: E402
from models.artifact import ArtifactDetail, ArtifactVersion  # noqa: E402
from models.common import ThreadStatus  # noqa: E402
from models.thread import Thread  # noqa: E402
from repositories.thread_repository import ThreadRepository  # noqa: E402
from services.thread_service import ThreadService  # noqa: E402

OWNER = auth.AuthUser(sub="sub-owner", username="owner", groups=["user"])
OTHER = auth.AuthUser(sub="sub-other", username="other", groups=["user"])
# Ownership does not bend for admin: a conversation is personal data, not
# platform configuration.
ADMIN = auth.AuthUser(sub="sub-admin", username="admin", groups=["admin"])

OWNED = "t-owned"
LEGACY = "t-legacy"


def _thread(thread_id, owner_sub):
    return Thread(
        thread_id=thread_id,
        created_at="2026-08-01T00:00:00",
        updated_at="2026-08-01T00:00:00",
        values={"messages": [{"id": "m1", "type": "human", "content": "hi"}]},
        metadata={},
        owner_sub=owner_sub,
    )


class StubRepository:
    """In-memory stand-in for ThreadRepository, faithful where it matters.

    Storage is dicts, not Thread objects, and create/update go through the real
    ThreadRepository's item-building code. That is the whole point: both are
    whole-item `put_item` calls, so an attribute the real code forgets to list
    is *erased* in DynamoDB. A stub that copied Thread fields across would make
    that silent-unown bug invisible — which it did, until this was rewritten.

    `search` filters by owner, as the real scan's FilterExpression does.
    """

    def __init__(self):
        self.items = {}
        for thread_id, owner in ((OWNED, OWNER.sub), (LEGACY, "")):
            self.items[thread_id] = self._as_item(_thread(thread_id, owner))

    @staticmethod
    def _as_item(thread):
        """Serialise exactly as ThreadRepository.create does."""
        return {
            "thread_id": thread.thread_id,
            "created_at": thread.created_at,
            "updated_at": thread.updated_at,
            "status": thread.status.value
            if isinstance(thread.status, ThreadStatus)
            else thread.status,
            "values": json.dumps(thread.values) if thread.values else "{}",
            "metadata": json.dumps(thread.metadata) if thread.metadata else "{}",
            "owner_sub": thread.owner_sub or "",
        }

    def get(self, thread_id):
        item = self.items.get(thread_id)
        return ThreadRepository._to_thread(item) if item else None

    def create(self, thread):
        self.items[thread.thread_id] = self._as_item(thread)
        return thread

    def update(self, thread_id, thread):
        # Runs the real update()'s item construction against a fake table, so a
        # field missing from its dict disappears here too.
        self.items[thread_id] = _real_update_item(thread_id, thread)
        return thread

    def delete(self, thread_id):
        self.items.pop(thread_id, None)

    def owner_of(self, thread_id):
        """Read the stored attribute, not a Thread field — an erased attribute
        must read as absent rather than as the model default."""
        return self.items[thread_id].get("owner_sub")

    def search(self, owner_sub, **kwargs):
        # owner_sub None means unscoped, as in the real repository, where it
        # omits the FilterExpression entirely. Only the admin path passes None.
        return [
            ThreadRepository._to_thread(i)
            for i in self.items.values()
            if owner_sub is None or i.get("owner_sub") == owner_sub
        ]


class _CapturingTable:
    """Captures the Item a repository method would have written."""

    def __init__(self):
        self.item = None

    def put_item(self, Item):  # noqa: N803 — boto3's parameter name
        self.item = Item


def _real_update_item(thread_id, thread):
    """The item ThreadRepository.update() builds, captured rather than sent.

    Calls the production method with its table swapped out, so the assertions
    below are about the real serialisation and not a copy of it that could drift.
    """
    repository = ThreadRepository.__new__(ThreadRepository)
    repository.table = _CapturingTable()
    ThreadRepository.update(repository, thread_id, thread)
    return repository.table.item


class StubStreamingService:
    """Runs the real ownership decision, without AgentCore.

    Delegates to the real get_or_create_thread rather than reimplementing the
    check — that call is where a stream into someone else's thread is refused.
    """

    def __init__(self, thread_service):
        self.thread_service = thread_service
        self.calls = []

    async def stream_thread_execution(
        self, thread_id, request, actor_id=None, owner_sub=""
    ):
        self.thread_service.get_or_create_thread(
            thread_id=thread_id, owner_sub=owner_sub, initial_values=None
        )
        self.calls.append((thread_id, actor_id, owner_sub))
        return {"ok": True}


VERSION = ArtifactVersion(
    artifact_id="a1", version=1, thread_id=OWNED, title="Doc", kind="markdown",
    s3_key="k", size_bytes=3, created_at="2026-08-01T00:00:00",
)


class StubArtifactService:
    def thread_of(self, artifact_id):
        return VERSION.thread_id

    def list_for_thread(self, thread_id):
        return [VERSION]

    def detail(self, artifact_id):
        return ArtifactDetail(latest=VERSION, versions=[1])

    def content(self, artifact_id, version):
        from models.artifact import ArtifactContent

        return ArtifactContent(
            artifact_id=artifact_id, version=version, kind="markdown",
            title="Doc", content="hi",
        )

    def share_url(self, artifact_id, version, expires_in=None, download=False):
        return ("https://example.invalid/signed", 60)


class StubAttachmentService:
    def store(self, thread_id, filename, body):
        from models.attachment import Attachment

        return Attachment(
            attachment_id="att1", filename=filename, media_type="image/png",
            size_bytes=len(body), kind="image",
        )

    def fetch(self, thread_id, attachment_id):
        return (b"bytes", "image/png", "a.png")


class StubBrowserScreenshotService:
    """Always finds a screenshot: these tests are about who may ask, not about
    whether the object exists. The 404 and 503 paths live in
    test_browser_screenshot_route.py."""

    def fetch(self, messages, tool_call_id):
        return b"\x89PNG\r\n\x1a\n"


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
    monkeypatch.setattr(
        thread_routes, "attachment_service", StubAttachmentService()
    )
    monkeypatch.setattr(
        thread_routes, "browser_screenshot_service", StubBrowserScreenshotService()
    )
    monkeypatch.setattr(artifact_routes, "thread_service", service)
    monkeypatch.setattr(artifact_routes, "artifact_service", StubArtifactService())

    application = FastAPI()
    application.include_router(thread_routes.router)
    application.include_router(artifact_routes.router)
    return application


def _client(app, user):
    app.dependency_overrides[auth.current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


# Every route that takes a thread id, with a body where one is required.
THREAD_ROUTES = [
    ("GET", "/threads/{tid}", None),
    ("GET", "/threads/{tid}/state", None),
    ("PATCH", "/threads/{tid}/state", {"values": {"a": 1}}),
    ("DELETE", "/threads/{tid}", None),
    ("POST", "/threads/{tid}/attachments", None),
    ("GET", "/threads/{tid}/attachments/att1", None),
    ("GET", "/threads/{tid}/browser-screenshots/tooluse_1", None),
    ("GET", "/api/artifacts/thread/{tid}", None),
]

ARTIFACT_ROUTES = [
    ("GET", "/api/artifacts/a1", None),
    ("GET", "/api/artifacts/a1/versions/1/content", None),
    ("POST", "/api/artifacts/a1/versions/1/share", None),
]


def _request(client, method, path, body):
    if method == "POST" and path.endswith("/attachments"):
        # Multipart, not JSON: the handler declares UploadFile.
        return client.post(path, files={"file": ("a.png", b"bytes", "image/png")})
    return client.request(method, path, json=body)


@pytest.mark.parametrize("method,template,body", THREAD_ROUTES + ARTIFACT_ROUTES,
                         ids=lambda v: str(v))
def test_the_owner_is_allowed(method, template, body, app):
    response = _request(
        _client(app, OWNER), method, template.format(tid=OWNED), body
    )

    assert response.status_code not in (401, 403, 404), response.text


@pytest.mark.parametrize("method,template,body", THREAD_ROUTES + ARTIFACT_ROUTES,
                         ids=lambda v: str(v))
def test_another_user_is_403(method, template, body, app):
    response = _request(
        _client(app, OTHER), method, template.format(tid=OWNED), body
    )

    assert response.status_code == 403, response.text


@pytest.mark.parametrize("method,template,body", THREAD_ROUTES + ARTIFACT_ROUTES,
                         ids=lambda v: str(v))
def test_admin_may_use_anyones_thread(method, template, body, app):
    """Admin reads and writes across accounts, as it does for knowledge bases.

    Streaming is excluded from these tables and covered separately below: it is
    the one route where the admin override would corrupt data rather than expose
    it.
    """
    response = _request(
        _client(app, ADMIN), method, template.format(tid=OWNED), body
    )

    assert response.status_code not in (401, 403), response.text


@pytest.mark.parametrize("method,template,body", THREAD_ROUTES,
                         ids=lambda v: str(v))
def test_a_missing_thread_is_404(method, template, body, app):
    response = _request(
        _client(app, OWNER), method, template.format(tid="t-nope"), body
    )

    assert response.status_code == 404, response.text


@pytest.mark.parametrize("method,template,body", THREAD_ROUTES,
                         ids=lambda v: str(v))
def test_an_unowned_legacy_thread_fails_closed(method, template, body, app):
    """A record from before ownership existed belongs to nobody, not everybody.

    This is the case the backfill script exists for: until it runs, the eight
    pre-existing threads are readable by no account at all.
    """
    response = _request(
        _client(app, OWNER), method, template.format(tid=LEGACY), body
    )

    assert response.status_code == 403, response.text


def test_the_list_shows_only_your_own(app, repo):
    repo.create(_thread("t-theirs", OTHER.sub))

    body = _client(app, OWNER).get("/threads").json()

    assert [t["thread_id"] for t in body] == [OWNED]


def test_the_list_hides_unowned_threads_from_plain_users(app):
    """LEGACY belongs to nobody, so no ordinary account lists it."""
    for user in (OWNER, OTHER):
        body = _client(app, user).get("/threads").json()

        assert LEGACY not in [t["thread_id"] for t in body]


def test_an_admin_lists_every_thread(app, repo):
    """Including the unowned ones — otherwise the sidebar would hide threads the
    per-thread routes will happily open for an admin."""
    repo.create(_thread("t-theirs", OTHER.sub))

    body = _client(app, ADMIN).get("/threads").json()

    assert {t["thread_id"] for t in body} == {OWNED, LEGACY, "t-theirs"}


def test_an_admin_may_read_an_unowned_legacy_thread(app):
    assert _client(app, ADMIN).get(f"/threads/{LEGACY}").status_code == 200


def test_creating_a_thread_stamps_the_caller(app, repo):
    body = _client(app, OWNER).post("/threads", json={"messages": []}).json()

    assert repo.owner_of(body["thread_id"]) == OWNER.sub


def test_the_body_cannot_choose_the_owner(app, repo):
    """POST /threads stores its body as `values`, so a forged owner field there
    must not reach the owner column."""
    body = (
        _client(app, OTHER)
        .post("/threads", json={"owner_sub": OWNER.sub})
        .json()
    )

    assert repo.owner_of(body["thread_id"]) == OTHER.sub


def test_a_metadata_patch_cannot_steal_a_thread(app, repo):
    """PATCH /state merges caller-supplied metadata into the thread, which is
    why the owner is a top-level field and not a key inside it."""
    response = _client(app, OWNER).patch(
        f"/threads/{OWNED}/state",
        json={"metadata": {"owner_sub": OTHER.sub}},
    )

    assert response.status_code == 200
    assert repo.owner_of(OWNED) == OWNER.sub


def test_update_persists_the_owner_attribute():
    """ThreadRepository.update is a whole-item put_item: an attribute absent from
    the dict it builds is erased in DynamoDB, so one PATCH would unown the
    thread and every later read would 403.

    Asserted against the item the production method actually writes. Going
    through the route instead would pass even with the field dropped, because a
    Thread reconstructed in memory still carries its default.
    """
    item = _real_update_item(OWNED, _thread(OWNED, OWNER.sub))

    assert item["owner_sub"] == OWNER.sub


def test_create_persists_the_owner_attribute():
    """Same for create(), the other whole-item write."""
    repository = ThreadRepository.__new__(ThreadRepository)
    repository.table = _CapturingTable()
    ThreadRepository.create(repository, _thread("t-new", OWNER.sub))

    assert repository.table.item["owner_sub"] == OWNER.sub


def test_the_owner_survives_a_state_write(app, repo):
    """The round trip, on top of the serialisation test above."""
    _client(app, OWNER).patch(f"/threads/{OWNED}/state", json={"values": {"a": 1}})

    assert repo.owner_of(OWNED) == OWNER.sub
    assert _client(app, OWNER).get(f"/threads/{OWNED}").status_code == 200


def test_a_status_write_keeps_the_owner(app, repo):
    """update_thread_status goes through the same put_item, and the streaming
    path calls it on every terminal state."""
    thread_routes.thread_service.update_thread_status(OWNED, ThreadStatus.IDLE)

    assert repo.owner_of(OWNED) == OWNER.sub


def test_streaming_into_someone_elses_thread_is_403(app, repo):
    response = _client(app, OTHER).post(f"/threads/{OWNED}/runs/stream", json={})

    assert response.status_code == 403
    # And nothing was appended to the owner's conversation.
    assert len(repo.get(OWNED).values["messages"]) == 1


def test_an_admin_still_cannot_stream_into_someone_elses_thread(app, repo):
    """The deliberate exception to the admin override.

    A run writes to AgentCore Memory under the caller's own actor id, so an
    admin streaming here would file another user's conversation into its own
    memory and answer with its own recall. Reading the thread is inspection;
    sending a turn is not.
    """
    for route in ("runs/stream", "stream"):
        response = _client(app, ADMIN).post(f"/threads/{OWNED}/{route}", json={})

        assert response.status_code == 403, route
    assert len(repo.get(OWNED).values["messages"]) == 1


def test_an_admin_can_stream_in_its_own_thread(app, repo):
    """The override's absence must not stop an admin from simply chatting."""
    repo.create(_thread("t-admin", ADMIN.sub))

    response = _client(app, ADMIN).post("/threads/t-admin/runs/stream", json={})

    assert response.status_code == 200


def test_streaming_a_new_thread_id_creates_it_owned(app, repo):
    response = _client(app, OTHER).post("/threads/t-fresh/runs/stream", json={})

    assert response.status_code == 200
    assert repo.owner_of("t-fresh") == OTHER.sub


def test_streaming_passes_the_actor_and_the_owner(app):
    """Both are the Cognito sub, and they are not interchangeable: actor_id
    scopes AgentCore Memory, owner_sub decides access."""
    _client(app, OWNER).post(f"/threads/{OWNED}/stream", json={})

    assert thread_routes.streaming_service.calls == [
        (OWNED, OWNER.sub, OWNER.sub)
    ]


def test_search_refuses_to_default_to_everyones_threads(service):
    """A caller that forgets the owner must fail, not list the whole table."""
    with pytest.raises(TypeError):
        service.search_threads()
