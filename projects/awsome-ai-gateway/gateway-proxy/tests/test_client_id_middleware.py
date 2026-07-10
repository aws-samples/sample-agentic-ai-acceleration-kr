# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ClientIdentificationMiddleware writes state['client'] from headers."""

import pytest

from app.middleware.client_id import ClientIdentificationMiddleware


@pytest.mark.asyncio
async def test_sets_client_on_state():
    captured = {}

    async def downstream(scope, receive, send):
        captured["client"] = scope["state"]["client"]

    mw = ClientIdentificationMiddleware(downstream)
    scope = {
        "type": "http",
        "path": "/v1/messages",
        "state": {},
        "headers": [(b"user-agent", b"claude-cli/2.1.177 (external, claude-desktop-3p)")],
    }
    await mw(scope, None, None)
    assert captured["client"] == "cowork"


@pytest.mark.asyncio
async def test_defaults_to_other_with_no_headers():
    captured = {}

    async def downstream(scope, receive, send):
        captured["client"] = scope["state"]["client"]

    mw = ClientIdentificationMiddleware(downstream)
    scope = {"type": "http", "path": "/v1/messages", "state": {}, "headers": []}
    await mw(scope, None, None)
    assert captured["client"] == "other"


@pytest.mark.asyncio
async def test_passes_through_non_http():
    called = {}

    async def downstream(scope, receive, send):
        called["hit"] = True

    mw = ClientIdentificationMiddleware(downstream)
    await mw({"type": "lifespan"}, None, None)
    assert called["hit"] is True
