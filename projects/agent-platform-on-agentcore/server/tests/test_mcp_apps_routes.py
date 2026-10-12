"""MCP Apps 릴레이 라우트.

기존 /api/mcp/tools/call은 브라우저가 보낸 server_config로 stdio 명령을 실행하기 때문에
require_admin이다. 앱 발신 호출은 일반 채팅 사용자가 발생시키므로 그 라우트를 재사용하면
임의 명령 실행이 일반 사용자에게 열린다. 그래서 라우트를 분리하고, 엔드포인트는
레지스트리에서만 해석한다 — 이 파일이 지키는 불변식이다.
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.mcp_apps as mcp_apps_routes  # noqa: E402
from mcp_core.ui_meta import APP_MIME_TYPE  # noqa: E402

USER = auth.AuthUser(sub="u-plain", username="bob", groups=["user"])


class StubRelay:
    """서버 측 릴레이 스텁. 호출 인자를 기록한다."""

    def __init__(self):
        self.read_calls = []
        self.call_calls = []
        self.tool_meta = {"ui": {"resourceUri": "ui://x/view", "visibility": ["model", "app"]}}

    def resolve_endpoint(self, record_id):
        if record_id == "missing":
            raise ValueError(f"MCP record '{record_id}' has no endpoint URL")
        return f"https://mcp.example/{record_id}/mcp"

    def read_ui_resource(self, record_id, uri):
        self.resolve_endpoint(record_id)  # Check record_id validity first
        self.read_calls.append((record_id, uri))
        return mcp_apps_routes.UiResourcePayload(
            uri=uri, mime_type=APP_MIME_TYPE, text="<html>app</html>",
            ui_meta={"prefersBorder": True},
        )

    def tool_meta_for(self, record_id, tool_name):
        self.resolve_endpoint(record_id)  # Check record_id validity first
        return self.tool_meta

    def call_tool(self, record_id, tool_name, arguments):
        self.call_calls.append((record_id, tool_name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}

    def describe_tool(self, record_id, tool_name):
        self.resolve_endpoint(record_id)
        if tool_name.split("___", 1)[-1] != "get_status":
            raise ValueError(f"'{tool_name}' 툴을 찾을 수 없습니다.")
        return {
            "name": tool_name,
            "description": "상태 조회",
            "inputSchema": {"type": "object", "properties": {}},
            "_meta": self.tool_meta,
        }

    def call_app_tool_checked(self, record_id, tool_name, arguments):
        """검사와 호출이 한 세션에서 일어나는 실제 경로를 흉내 낸다.

        visibility 판정을 스텁에서 흉내 내지 않고 실제 is_app_callable을 쓴다 —
        스텁이 판정까지 대신하면 규격 MUST를 검증하지 못하는 고무도장이 된다.
        """
        from mcp_core.ui_meta import is_app_callable

        self.resolve_endpoint(record_id)
        if not is_app_callable(self.tool_meta):
            raise PermissionError(f"'{tool_name}' 툴은 앱에서 호출할 수 없습니다.")
        return self.call_tool(record_id, tool_name, arguments)


@pytest.fixture
def client(monkeypatch):
    relay = StubRelay()
    monkeypatch.setattr(mcp_apps_routes, "_relay", lambda: relay)

    app = FastAPI()
    app.include_router(mcp_apps_routes.router)
    app.dependency_overrides[auth.current_user] = lambda: USER

    test_client = TestClient(app)
    test_client.relay = relay
    return test_client


def test_read_resource_returns_html_and_ui_meta(client):
    resp = client.post("/api/mcp-apps/resources/read",
                       json={"record_id": "r1", "uri": "ui://x/view"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["text"] == "<html>app</html>"
    assert body["mime_type"] == APP_MIME_TYPE
    assert body["ui_meta"] == {"prefersBorder": True}
    assert client.relay.read_calls == [("r1", "ui://x/view")]


def test_read_resource_rejects_non_ui_scheme(client):
    """호스트가 임의 URL을 가져오게 만들 수 없어야 한다."""
    resp = client.post("/api/mcp-apps/resources/read",
                       json={"record_id": "r1", "uri": "https://evil.example/x"})

    assert resp.status_code == 400
    assert client.relay.read_calls == []


def test_read_resource_unknown_record_is_400(client):
    resp = client.post("/api/mcp-apps/resources/read",
                       json={"record_id": "missing", "uri": "ui://x/view"})
    assert resp.status_code == 400


def test_tool_call_relays_when_app_visible(client):
    resp = client.post("/api/mcp-apps/tools/call",
                       json={"record_id": "r1", "tool_name": "refresh",
                             "arguments": {"a": 1}})

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert client.relay.call_calls == [("r1", "refresh", {"a": 1})]


def test_tool_call_rejected_for_model_only_tool(client):
    """규격 MUST: app이 visibility에 없으면 앱 발신 호출을 거부한다."""
    client.relay.tool_meta = {"ui": {"visibility": ["model"]}}

    resp = client.post("/api/mcp-apps/tools/call",
                       json={"record_id": "r1", "tool_name": "secret",
                             "arguments": {}})

    assert resp.status_code == 403
    assert client.relay.call_calls == []


def test_tool_call_allowed_when_visibility_absent(client):
    """기본값이 ["model","app"]이므로 _meta 없는 기존 툴은 그대로 호출된다."""
    client.relay.tool_meta = None

    resp = client.post("/api/mcp-apps/tools/call",
                       json={"record_id": "r1", "tool_name": "legacy",
                             "arguments": {}})

    assert resp.status_code == 200
    assert client.relay.call_calls == [("r1", "legacy", {})]


def test_routes_never_accept_server_config(client):
    """server_config를 본문으로 받으면 임의 명령 실행 경로가 열린다.

    Pydantic 모델에 그 필드가 없어야 하고, 보내도 무시돼야 한다.
    """
    from models.mcp_apps import McpAppResourceReadRequest, McpAppToolCallRequest

    assert "server_config" not in McpAppResourceReadRequest.model_fields
    assert "server_config" not in McpAppToolCallRequest.model_fields

    resp = client.post(
        "/api/mcp-apps/tools/call",
        json={"record_id": "r1", "tool_name": "refresh", "arguments": {},
              "server_config": {"command": "rm", "args": ["-rf", "/"]}},
    )
    assert resp.status_code == 200
    assert client.relay.call_calls == [("r1", "refresh", {})]


def test_session_verdict_is_returned_not_raised():
    """세션 안에서 raise하면 anyio가 ExceptionGroup으로 감싸 403이 502가 된다.

    실제 MCP 서버로 검증하다 발견한 버그다. _session_list_and_call은 판정을 값으로
    돌려주고, 예외 변환은 세션 밖에서 일어나야 한다. 스텁 테스트만으로는 잡히지
    않으므로 계약 자체를 고정해 둔다.
    """
    import inspect

    from services.mcp_apps_service import McpAppsRelay

    source = inspect.getsource(McpAppsRelay._session_list_and_call)
    assert "return \"not_found\", None" in source
    assert "return \"forbidden\", None" in source
    # 세션 컨텍스트 안에서 이 두 예외를 직접 올리면 안 된다.
    assert "raise PermissionError" not in source
    assert "raise ValueError" not in source


def test_checked_call_maps_verdicts_to_exceptions(monkeypatch):
    """판정 → 예외 매핑이 라우트가 기대하는 타입이어야 한다 (403/400)."""
    from services.mcp_apps_service import McpAppsRelay

    relay = McpAppsRelay(registry=object())
    relay.resolve_endpoint = lambda rid: "http://example/mcp"

    monkeypatch.setattr(
        relay, "_session_list_and_call", lambda *a, **k: ("forbidden", None)
    )
    with pytest.raises(PermissionError):
        relay.call_app_tool_checked("r", "model_only", {})

    monkeypatch.setattr(
        relay, "_session_list_and_call", lambda *a, **k: ("not_found", None)
    )
    with pytest.raises(ValueError):
        relay.call_app_tool_checked("r", "nope", {})

    monkeypatch.setattr(
        relay, "_session_list_and_call", lambda *a, **k: ("ok", {"content": []})
    )
    assert relay.call_app_tool_checked("r", "fine", {}) == {"content": []}


def test_bad_record_id_is_a_client_error_not_502():
    """브라우저가 보낸 record_id가 잘못되면 400이어야 한다.

    배포 환경에서 잘못된 형식의 record_id가 AWS ValidationException을 일으켜 502로
    나갔고, 응답 본문에 AWS 내부 메시지가 그대로 실렸다.
    """
    from botocore.exceptions import ClientError

    from services.mcp_apps_service import McpAppsRelay

    class BadRegistry:
        def __init__(self, code):
            self._code = code

        def get_record(self, record_id):
            raise ClientError(
                {"Error": {"Code": self._code, "Message": "boom"}}, "GetRegistryRecord"
            )

    for code in ("ValidationException", "ResourceNotFoundException"):
        relay = McpAppsRelay(registry=BadRegistry(code))
        with pytest.raises(ValueError):
            relay.resolve_endpoint("bad id")

    # 그 밖의 AWS 오류는 감추지 않는다 — 502가 맞는 상황이다.
    relay = McpAppsRelay(registry=BadRegistry("AccessDeniedException"))
    with pytest.raises(ClientError):
        relay.resolve_endpoint("r1")


def test_root_causes_unwraps_exception_groups():
    """502 본문이 "unhandled errors in a TaskGroup"으로 뭉개지면 진단이 불가능하다.

    MCP 세션은 anyio task group 안에서 열리므로 안에서 난 오류가 감싸진다.
    배포 환경에서 SigV4 누락을 찾는 데 이 때문에 시간을 썼다.
    """
    from routes.mcp_apps import _root_causes

    nested = BaseExceptionGroup(
        "unhandled errors in a TaskGroup",
        [BaseExceptionGroup("inner", [RuntimeError("403 Forbidden")])],
    )
    assert _root_causes(nested) == "RuntimeError: 403 Forbidden"

    multi = BaseExceptionGroup("g", [ValueError("a"), RuntimeError("b")])
    assert _root_causes(multi) == "ValueError: a / RuntimeError: b"

    # 평범한 예외는 그대로 표현한다.
    assert _root_causes(ValueError("plain")) == "ValueError: plain"


def test_tool_describe_returns_the_tool_definition(client):
    """hostContext.toolInfo 용: 규격의 toolInfo.tool 은 name·inputSchema 를 가진 툴 정의다."""
    resp = client.post("/api/mcp-apps/tools/describe",
                       json={"record_id": "r1", "tool_name": "get_status"})

    assert resp.status_code == 200
    tool = resp.json()["tool"]
    assert tool["name"] == "get_status"
    assert tool["inputSchema"] == {"type": "object", "properties": {}}
    assert tool["_meta"]["ui"]["resourceUri"] == "ui://x/view"


def test_tool_describe_keeps_the_gateway_prefixed_name(client):
    """게이트웨이 이름으로 부른 앱은 그 이름으로 다시 tools/call 을 하므로 바꾸지 않는다."""
    resp = client.post("/api/mcp-apps/tools/describe",
                       json={"record_id": "r1", "tool_name": "target___get_status"})

    assert resp.status_code == 200
    assert resp.json()["tool"]["name"] == "target___get_status"


def test_tool_describe_unknown_tool_is_400(client):
    resp = client.post("/api/mcp-apps/tools/describe",
                       json={"record_id": "r1", "tool_name": "nope"})
    assert resp.status_code == 400


def test_tool_describe_unknown_record_is_400(client):
    resp = client.post("/api/mcp-apps/tools/describe",
                       json={"record_id": "missing", "tool_name": "get_status"})
    assert resp.status_code == 400


def test_relay_routes_are_sync_not_async():
    """이 핸들러들은 반드시 동기(`def`)여야 한다 — `async def`면 안 된다.

    릴레이 메서드는 `open_session` 안에서 블로킹 포털 호출로 MCP 세션을 연다.
    `async def` 라우트는 이벤트 루프 스레드에서 돌기 때문에 그 블로킹이 서버 전체를
    정지시킨다: 배포에서 콜드 스타트한 tools/call 하나가 60초 루프를 잡자 재시도가
    쌓이며 단일 워커가 완전히 얼어붙었고 헬스 체크까지 504가 났다. `def`로 두면
    FastAPI가 스레드풀에서 돌려 이벤트 루프가 자유로워진다. 리팩터가 실수로 다시
    `async def`로 돌리면 이 테스트가 잡는다.
    """
    import inspect

    from routes import mcp_apps as routes

    for handler in (routes.read_ui_resource, routes.call_app_tool, routes.describe_app_tool):
        assert not inspect.iscoroutinefunction(handler), (
            f"{handler.__name__} must be a sync def so blocking MCP I/O runs in "
            "FastAPI's threadpool, not on the event loop"
        )
