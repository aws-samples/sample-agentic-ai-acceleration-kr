"""A failed connection / first page is retried once before the session gives up
its gateway tools (page-level retries are in test_mcp_tools_partial_pages.py).

Under 300 concurrent new chats the load failed transiently (2026-10-18), and
each such session silently ran with local tools only.
"""
import core.mcp_manager as mm


class Cfg:
    mcp_server_url = "https://gw.example/mcp"


def test_a_transient_failure_is_retried(monkeypatch):
    calls = []

    def flaky(url, session_scope=None):
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("ListToolsResult validation error")
        return ["tool"], object()

    monkeypatch.setattr(mm.access_token, "load_tools_from_mcp", flaky)
    monkeypatch.setattr(mm.time, "sleep", lambda s: None)
    monkeypatch.setattr(mm, "filter_model_visible", lambda t: t)
    monkeypatch.setattr(mm, "drop_artifact_tools", lambda t: t)
    manager = mm.MCPServerManager(Cfg())
    monkeypatch.setattr(manager, "_log_available_tools", lambda t: None)
    tools, client = manager.load_tools()
    assert tools == ["tool"] and len(calls) == 2


def test_it_still_gives_up_after_the_attempts(monkeypatch):
    calls = []
    monkeypatch.setattr(mm.access_token, "load_tools_from_mcp",
                        lambda url, session_scope=None: calls.append(1) or (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(mm.time, "sleep", lambda s: None)
    monkeypatch.setenv("MCP_TOOLS_ATTEMPTS", "2")
    assert mm.MCPServerManager(Cfg()).load_tools() == (None, None)
    assert len(calls) == 2
