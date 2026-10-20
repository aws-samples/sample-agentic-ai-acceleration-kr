"""The Slack Web API wrapper (integrations/slack/api.py)."""
import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.slack.api import API_BASE, SlackApi, SlackApiError  # noqa: E402

TOKEN = "xoxb-123-secret-value"


def _api(handler):
    client = httpx.AsyncClient(base_url=API_BASE, transport=httpx.MockTransport(handler))
    api = SlackApi(client=client)
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    api._sleep = fake_sleep
    return api, waits


def test_the_token_goes_in_the_header_and_scopes_are_read():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True, "team_id": "T1"}, headers={"x-oauth-scopes": "chat:write, im:history"})

    api, _ = _api(handler)
    resp = asyncio.run(api.auth_test(TOKEN))
    assert seen == {"auth": f"Bearer {TOKEN}", "path": "/api/auth.test"}
    assert resp.data["team_id"] == "T1"
    assert resp.scopes == {"chat:write", "im:history"}


def test_ok_false_raises_with_slacks_code_and_never_the_token():
    api, _ = _api(lambda request: httpx.Response(200, json={"ok": False, "error": "invalid_auth"}))
    with pytest.raises(SlackApiError) as caught:
        asyncio.run(api.auth_test(TOKEN))
    assert caught.value.error == "invalid_auth"
    assert caught.value.is_auth_error
    assert TOKEN not in str(caught.value) and "secret" not in repr(caught.value.args)


def test_429_waits_retry_after_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True, "ts": "1.2"})

    api, waits = _api(handler)
    assert asyncio.run(api.post_message(TOKEN, "D1", text="hi")) == "1.2"
    assert waits == [2.0, 2.0]


def test_429_with_a_long_retry_after_is_given_back_to_the_caller():
    api, waits = _api(lambda request: httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(SlackApiError) as caught:
        asyncio.run(api.post_message(TOKEN, "D1", text="hi"))
    assert caught.value.error == "rate_limited"
    assert waits == []


def test_network_errors_carry_no_request_detail():
    def handler(request):
        raise httpx.ConnectError(f"failed for {request.headers['authorization']}")

    api, _ = _api(handler)
    with pytest.raises(SlackApiError) as caught:
        asyncio.run(api.auth_test(TOKEN))
    assert caught.value.error == "network_error"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_markdown_text_and_thread_ts_are_sent_form_encoded():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True, "ts": "9.9"})

    api, _ = _api(handler)
    asyncio.run(api.post_message(TOKEN, "D1", thread_ts="1.0", markdown_text="**hi**"))
    assert "markdown_text=%2A%2Ahi%2A%2A" in seen["body"]
    assert "thread_ts=1.0" in seen["body"]
    assert "text=" not in seen["body"].replace("markdown_text=", "")
