"""One failing tools/list page no longer loses every gateway tool.

The gateway's `registry` target answered 429 under load and its page error used
to fail the whole load: 442 of 650 sessions had no gateway tools at 300
concurrent new chats (2026-10-18)."""
import pytest

import auth.access_token as at


class Page(list):
    def __init__(self, items, token):
        super().__init__(items)
        self.pagination_token = token


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __enter__(self):
        return self

    def list_tools_sync(self, pagination_token=None):
        self.calls.append(pagination_token)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _patch(monkeypatch, client):
    import strands.tools.mcp as smcp
    monkeypatch.setattr(smcp, "MCPClient", lambda factory: client)
    monkeypatch.setattr(at, "sigv4_httpx_client_factory", lambda url: None)
    monkeypatch.setattr(at.time, "sleep", lambda s: None)


def test_a_flaky_page_is_retried(monkeypatch):
    client = FakeClient([Page(["a"], "p2"), RuntimeError("429"), Page(["b"], None)])
    _patch(monkeypatch, client)
    tools, _ = at.load_tools_from_mcp("https://gw/mcp")
    assert tools == ["a", "b"] and client.calls == [None, "p2", "p2"]


def test_a_page_that_keeps_failing_keeps_the_earlier_tools(monkeypatch):
    err = RuntimeError("registry 429")
    client = FakeClient([Page(["a", "b"], "p2"), err, err, err])
    _patch(monkeypatch, client)
    tools, _ = at.load_tools_from_mcp("https://gw/mcp")
    assert tools == ["a", "b"]


def test_a_failing_first_page_still_raises(monkeypatch):
    err = RuntimeError("down")
    _patch(monkeypatch, FakeClient([err, err, err]))
    with pytest.raises(RuntimeError):
        at.load_tools_from_mcp("https://gw/mcp")
