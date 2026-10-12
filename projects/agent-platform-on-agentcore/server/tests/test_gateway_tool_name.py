"""게이트웨이가 접두사 붙인 툴 이름으로도 앱 발신 호출이 닿는지.

AgentCore 게이트웨이는 타깃 이름을 `<target>___<tool>` 형태로 툴 이름에 붙인다.
에이전트는 그 이름으로 호출하므로 앱 이벤트에도 접두사가 실려 온다. 그런데 릴레이는
원본 MCP 서버에 직접 붙으므로 그 서버에는 그런 이름이 없다 — 실측에서 앱이 400
"'platform-status-app___get_platform_status' 툴을 찾을 수 없습니다"를 받았다.

`open_session`만 스텁한다: 검증하려는 것은 이름 해석과 visibility 판정이 **한 세션
안에서** 일어나는지이므로, `_session_list_and_call` 자체는 진짜 코드로 돌려야 한다.
"""
import os
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_core.app_session as app_session  # noqa: E402
from services.mcp_apps_service import McpAppsRelay  # noqa: E402


class StubTool:
    def __init__(self, name, visibility):
        self.name = name
        self.meta = {"ui": {"resourceUri": "ui://x/app.html", "visibility": visibility}}


class StubToolList:
    def __init__(self, tools):
        self.tools = tools


class StubSession:
    def __init__(self, tools):
        self._tools = tools
        self.called = []

    def list_tools(self):
        return StubToolList(self._tools)

    def call_tool(self, name, arguments):
        self.called.append((name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}


@pytest.fixture
def session(monkeypatch):
    """서버에는 접두사 없는 이름만 존재한다 — 게이트웨이가 붙이는 쪽은 서버가 모른다."""
    stub = StubSession(
        [
            StubTool("get_platform_status", ["model", "app"]),
            StubTool("refresh_platform_status", ["app"]),
            StubTool("secret_status", ["model"]),
        ]
    )

    @contextmanager
    def fake_open_session(url):
        yield stub

    monkeypatch.setattr(app_session, "open_session", fake_open_session)
    return stub


def relay_for(url="https://mcp.example/apps/mcp"):
    relay = McpAppsRelay(registry=object())
    relay.resolve_endpoint = lambda record_id: url
    return relay


def test_a_gateway_prefixed_name_calls_the_servers_own_tool(session):
    relay_for().call_app_tool_checked(
        "apps", "platform-status-app___get_platform_status", {}
    )

    assert session.called == [("get_platform_status", {})]


def test_an_unprefixed_name_is_called_as_is(session):
    relay_for().call_app_tool_checked("apps", "refresh_platform_status", {})

    assert session.called == [("refresh_platform_status", {})]


def test_visibility_is_judged_on_the_resolved_tool(session):
    """접두사를 벗긴 뒤의 툴로 판정해야 한다.

    벗기기 전 이름으로 판정하면 모델 전용 툴이 앱에서 호출 가능해진다 — 규격 MUST
    위반이고, 접두사 처리를 넣으면서 생기기 쉬운 사고다.
    """
    with pytest.raises(PermissionError):
        relay_for().call_app_tool_checked("apps", "gw___secret_status", {})

    assert session.called == []


def test_a_name_that_exists_nowhere_is_still_not_found(session):
    with pytest.raises(ValueError):
        relay_for().call_app_tool_checked("apps", "gw___nope", {})

    assert session.called == []
