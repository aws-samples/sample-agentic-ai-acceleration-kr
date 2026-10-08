"""Serving a browser screenshot back into a reopened thread.

The presigned URL the gateway Lambda returns lasts an hour, so a stored transcript
needs the S3 key instead — see services/browser_screenshot_service.py. What is
pinned here is the lookup: the key comes out of the thread's *own* stored messages
and never off the wire, which is what makes thread ownership a sufficient check.

The tool-result fixtures are the real wire shape, copied from an actual
InvokeHarness run against test_builtin_harness_agent: one plain JSON string, not
double-encoded, carrying both `url` and `s3_key`. A hand-rounded fixture would not
have caught that the gateway splits a long result across deltas, which is the bug
next door in [[project-harness-tool-result-deltas]].
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
from models.thread import Thread  # noqa: E402
from services.browser_screenshot_service import (  # noqa: E402
    BrowserScreenshotService,
    ScreenshotNotFound,
    ScreenshotsNotConfigured,
    find_screenshot_key,
    screenshot_key,
)

OWNER = auth.AuthUser(sub="sub-owner", username="owner", groups=["user"])
OTHER = auth.AuthUser(sub="sub-other", username="other", groups=["user"])

THREAD_ID = "t-owned"
TOOL_CALL_ID = "tooluse_aRu1v9NS2gKk8vLJu3oerb"
KEY = "screenshots/bap-bap-harnes-80fd020a/1786684605893.png"
PNG = b"\x89PNG\r\n\x1a\nfake"

# Exactly what arrives as `result` on the stored tool call. The presigned URL is
# abbreviated; everything else is verbatim.
WIRE_RESULT = json.dumps(
    {
        "session": "bap-harnes-80fd020a",
        "status": "SUCCESS",
        "bytes": 280,
        "url": "https://bap-builtin-tools-123456789012.s3.amazonaws.com/"
        f"{KEY}?AWSAccessKeyId=ASIA5JMSTZPLTHVQDNBK&Signature=abc&Expires=1786688206",
        "expires_in_seconds": 3600,
        "s3_key": KEY,
    }
)


def _messages(result=WIRE_RESULT, tool_call_id=TOOL_CALL_ID):
    return [
        {"id": "m1", "type": "human", "content": "screenshot the page"},
        {
            "id": "m2",
            "type": "ai",
            "content": "Here it is.",
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "name": "builtin-tools___browser_screenshot",
                    "args": {},
                    "status": "completed",
                    "result": result,
                }
            ],
        },
    ]


# --- key lookup ---------------------------------------------------------------


def test_the_key_is_read_off_the_real_wire_shape():
    assert screenshot_key(WIRE_RESULT) == KEY


def test_a_result_that_is_already_a_dict_works_too():
    """Not every producer stringifies: the persistence loop stores what it got."""
    assert screenshot_key(json.loads(WIRE_RESULT)) == KEY


@pytest.mark.parametrize(
    "result",
    [
        # Every other tool's result comes through the same field.
        "3 rows",
        '{"session":"s","status":"SUCCESS","bytes":280}',  # pre-s3_key Lambda
        '{"error":"screenshot failed"}',
        json.dumps({"s3_key": 42}),
        None,
        {},
    ],
    ids=["plain-text", "no-s3_key", "error", "non-string-key", "none", "empty"],
)
def test_anything_that_is_not_a_screenshot_yields_no_key(result):
    assert screenshot_key(result) is None


@pytest.mark.parametrize(
    "key",
    [
        "attachments/t-other/att1.png",
        "screenshots/../attachments/t-other/att1.png",
        "/etc/passwd",
    ],
    ids=["other-prefix", "traversal", "absolute"],
)
def test_a_key_outside_the_screenshot_prefix_is_refused(key):
    """The bucket holds only screenshots today, but the prefix is the contract:
    a tampered or buggy tool result must not become a general S3 reader."""
    assert screenshot_key(json.dumps({"s3_key": key})) is None


def test_the_newest_matching_tool_call_wins():
    """A re-run thread can repeat a tool call id; the recent capture is meant."""
    older = _messages(result=json.dumps({"s3_key": "screenshots/s/1.png"}))
    newer = _messages(result=json.dumps({"s3_key": "screenshots/s/2.png"}))

    assert find_screenshot_key(older + newer, TOOL_CALL_ID) == "screenshots/s/2.png"


def test_an_unknown_tool_call_id_yields_no_key():
    assert find_screenshot_key(_messages(), "tooluse_nope") is None


# --- service ------------------------------------------------------------------


class StubS3:
    def __init__(self, objects):
        self.objects = objects
        self.gets = []

    def get_object(self, Bucket, Key):
        self.gets.append((Bucket, Key))
        if Key not in self.objects:
            raise KeyError("NoSuchKey")
        return {"Body": _Body(self.objects[Key])}


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


def _service(objects=None, bucket="shots-bucket"):
    service = BrowserScreenshotService(bucket=bucket, region="us-east-1")
    service._s3 = StubS3(objects if objects is not None else {KEY: PNG})
    return service


def test_the_service_reads_the_object_the_stored_key_names():
    service = _service()

    assert service.fetch(_messages(), TOOL_CALL_ID) == PNG
    assert service.s3.gets == [("shots-bucket", KEY)]


def test_an_unconfigured_bucket_raises_rather_than_guessing():
    service = BrowserScreenshotService(bucket="", region="us-east-1")

    with pytest.raises(ScreenshotsNotConfigured):
        service.fetch(_messages(), TOOL_CALL_ID)


def test_a_vanished_object_is_not_found_rather_than_a_crash():
    """Lifecycle rules reach these objects, so a gone screenshot is ordinary."""
    service = _service(objects={})

    with pytest.raises(ScreenshotNotFound):
        service.fetch(_messages(), TOOL_CALL_ID)


# --- route --------------------------------------------------------------------


class StubThreadService:
    def require_owned(self, thread_id, sub, is_admin=False):
        from services.thread_service import ThreadForbidden, ThreadNotFound

        if thread_id != THREAD_ID:
            raise ThreadNotFound(f"Thread {thread_id} not found")
        if sub != OWNER.sub and not is_admin:
            raise ThreadForbidden("Not your thread")
        return Thread(
            thread_id=THREAD_ID,
            created_at="2026-08-01T00:00:00",
            updated_at="2026-08-01T00:00:00",
            values={"messages": _messages()},
            metadata={},
            owner_sub=OWNER.sub,
        )


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(thread_routes, "thread_service", StubThreadService())
    monkeypatch.setattr(thread_routes, "browser_screenshot_service", _service())

    application = FastAPI()
    application.include_router(thread_routes.router)
    return application


def _get(app, user, tool_call_id=TOOL_CALL_ID, thread_id=THREAD_ID):
    app.dependency_overrides[auth.current_user] = lambda: user
    client = TestClient(app, raise_server_exceptions=False)
    return client.get(f"/threads/{thread_id}/browser-screenshots/{tool_call_id}")


def test_the_owner_gets_the_png_inline(app):
    response = _get(app, OWNER)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG
    # Inline in a chat bubble, never a download: a filename of
    # "1786684605893.png" helps nobody.
    assert "content-disposition" not in response.headers


def test_another_user_is_403(app):
    assert _get(app, OTHER).status_code == 403


def test_a_tool_call_with_no_screenshot_is_404(app):
    assert _get(app, OWNER, tool_call_id="tooluse_nope").status_code == 404


def test_an_unconfigured_server_is_503(app, monkeypatch):
    monkeypatch.setattr(
        thread_routes,
        "browser_screenshot_service",
        BrowserScreenshotService(bucket="", region="us-east-1"),
    )

    assert _get(app, OWNER).status_code == 503
