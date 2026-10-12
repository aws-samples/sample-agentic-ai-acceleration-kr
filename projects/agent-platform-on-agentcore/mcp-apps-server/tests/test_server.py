"""서버가 호스트에게 약속하는 계약을 인메모리 세션으로 검증한다.

네트워크 없이 `server.py`의 MCPServer에 클라이언트를 직접 붙인다. 잡아야 하는 것은
호스트·릴레이가 의존하는 표면이다: 툴 집합, `_meta.ui`(resourceUri·visibility),
`resources/read`의 MIME 프로필. CloudWatch는 스텁으로 갈아 끼운다.

실행 (mcp-apps-server/ 에서):
    uv run --with 'mcp>=2' --with boto3 --with pytest --with anyio python -m pytest tests -q

`mcp.Client(server)`는 SDK 2.x의 인프로세스 연결이다 (1.x의
`create_connected_server_and_client_session`은 없어졌다). server.py 자체가 2.x 모듈
경로(`mcp.server.mcpserver`)를 쓰므로 requirements.txt 도 `mcp>=2` 로 고정한다.
"""
from __future__ import annotations

import os
import sys

import pytest
from mcp import Client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv  # noqa: E402

APP_MIME = "text/html;profile=mcp-app"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def stub_cloudwatch(monkeypatch):
    class _Stub:
        def get_metric_data(self, **kwargs):
            start = kwargs["StartTime"]
            return {
                "MetricDataResults": [
                    {
                        "Id": "q0_invocations",
                        "Label": "us.anthropic.claude-opus-5",
                        "Timestamps": [start],
                        "Values": [3.0],
                        "StatusCode": "Complete",
                    }
                ]
            }

    monkeypatch.setattr(srv, "_cloudwatch", lambda: _Stub())


def _ui_meta(tool) -> dict:
    return (tool.meta or {}).get("ui") or {}


async def test_exactly_two_tools_share_the_one_app_resource():
    """모델용 진입점과 앱 전용 질의만 있다. show_content·get_platform_status는 없다."""
    async with Client(srv.server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    assert set(tools) == {"get_platform_telemetry", "query_platform_telemetry"}
    for t in tools.values():
        assert _ui_meta(t)["resourceUri"] == srv.VIEW_URI


async def test_model_sees_only_the_entry_tool():
    """앱 전용 툴은 모델 툴 목록에서 빠져야 한다(규격 MUST) — 릴레이 is_app_callable이 의존."""
    async with Client(srv.server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    assert "model" in _ui_meta(tools["get_platform_telemetry"]).get("visibility", ["model", "app"])
    assert _ui_meta(tools["query_platform_telemetry"])["visibility"] == ["app"]


async def test_both_tools_take_view_and_period_with_defaults():
    async with Client(srv.server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    for name in ("get_platform_telemetry", "query_platform_telemetry"):
        props = tools[name].input_schema["properties"]
        assert set(props) >= {"view", "period"}
        assert set(props["view"]["enum"]) == {"models", "agents", "tools"}
        assert set(props["period"]["enum"]) == {"1h", "24h", "7d"}
        assert props["view"]["default"] == "models" and props["period"]["default"] == "24h"
        assert tools[name].input_schema.get("required", []) == []


async def test_resource_is_the_telemetry_app_html():
    """호스트는 MIME 프로필로 앱 HTML임을 판정한다 — 없으면 앱이 아니라 문서로 취급한다."""
    async with Client(srv.server) as client:
        result = await client.read_resource(srv.VIEW_URI)

    [content] = result.contents
    assert content.mime_type == APP_MIME
    assert "<!DOCTYPE html>" in content.text
    assert "<canvas" in content.text and 'id="tabs"' in content.text


async def test_tool_result_is_structured_telemetry_with_a_text_note():
    """앱은 structuredContent로 그리고, 앱 없는 클라이언트는 note를 읽는다."""
    async with Client(srv.server) as client:
        result = await client.call_tool(
            "get_platform_telemetry", {"view": "models", "period": "1h"}
        )

    assert not result.is_error
    data = result.structured_content
    assert data["view"] == "models" and data["period"] == "1h" and len(data["buckets"]) == 12
    assert data["series"][0]["label"] == "us/claude-opus-5"
    assert "note" in data and "1 models" in data["note"]
