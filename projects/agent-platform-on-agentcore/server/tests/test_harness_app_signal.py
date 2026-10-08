"""harness 에이전트도 앱 마운트 신호를 낸다.

런타임 에이전트는 `agent-runtime/main.py`가 툴 객체의 `_meta`를 읽어 `mcpApp`을
직접 발행한다. harness 는 그 코드를 지나지 않는다 — AWS 가 실행하고 서버는
`InvokeHarness` 의 Converse 이벤트만 받으므로, 툴 정의도 `_meta` 도 볼 수 없다.
그래서 harness 로 만든 에이전트에서는 앱이 아예 렌더링되지 않았다.

해법은 스트림에 있는 유일한 단서인 **툴 이름**으로 되찾는 것이다. 서버는 이미
레지스트리의 MCP 레코드에서 툴 목록을 읽을 수 있고(`McpAppsRelay`), 그 목록에
`_meta.ui.resourceUri` 가 실려 온다. 즉 `record_for_resource` 의 역방향이다.

툴 이름이 게이트웨이 접두사(`<target>___<tool>`)를 달고 오는 것도 같이 다룬다 —
harness 는 MCP 레코드를 게이트웨이 도구로 붙이므로 이 경로에서는 그게 기본값이다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.registry import RegistryRecordSummary  # noqa: E402
from services.mcp_apps_service import McpAppsRelay  # noqa: E402

APP_URI = "ui://platform-status/app.html"


class StubTool:
    def __init__(self, name, resource_uri=None):
        self.name = name
        self.meta = (
            {"ui": {"resourceUri": resource_uri, "visibility": ["model", "app"]}}
            if resource_uri
            else None
        )


class StubRegistry:
    def __init__(self, record_ids):
        self._record_ids = record_ids

    def list_records(self, descriptor_type=None, status=None, name=None):
        return [
            RegistryRecordSummary(
                record_id=rid, name=rid, descriptor_type="MCP", status="APPROVED"
            )
            for rid in self._record_ids
        ]


class ProbeRelay(McpAppsRelay):
    """세션을 열지 않고 툴 목록을 흉내 낸다."""

    def __init__(self, registry, tools_by_record, endpointless=()):
        super().__init__(registry=registry)
        self._tools_by_record = tools_by_record
        self._endpointless = set(endpointless)

    def resolve_endpoint(self, record_id):
        if record_id in self._endpointless:
            raise ValueError(f"MCP record '{record_id}' has no endpoint URL")
        return f"https://mcp.example/{record_id}/mcp"

    def _session_list_tools(self, url):
        return self._tools_by_record.get(url.split("/")[-2], [])


def relay():
    return ProbeRelay(
        StubRegistry(["broken", "apps"]),
        {
            "apps": [
                StubTool("get_platform_status", APP_URI),
                StubTool("plain_tool"),
            ]
        },
        endpointless=["broken"],
    )


def test_a_tool_name_resolves_to_its_app():
    assert relay().app_for_tool("get_platform_status") == ("apps", APP_URI)


def test_a_gateway_prefixed_tool_name_resolves_too():
    """harness 는 MCP 레코드를 게이트웨이 도구로 붙이므로 접두사가 기본값이다."""
    resolved = relay().app_for_tool("platform-status-app___get_platform_status")

    assert resolved == ("apps", APP_URI)


def test_a_tool_without_an_app_resolves_to_nothing():
    assert relay().app_for_tool("plain_tool") is None


def test_an_unknown_tool_resolves_to_nothing():
    assert relay().app_for_tool("nope") is None


def test_a_record_without_an_endpoint_does_not_stop_the_search():
    """엔드포인트 없는 MCP 레코드가 앞에 있어도 탐색이 끝까지 간다."""
    assert relay().app_for_tool("get_platform_status") is not None
