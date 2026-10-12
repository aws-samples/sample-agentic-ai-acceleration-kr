"""앱을 제공하는 MCP 레코드를 서버가 찾아 스트림 이벤트에 채운다.

왜 서버가 채우는가: `agent-runtime`은 MCP 서버에만 연결하므로 레지스트리 record_id를
모른다. 그래서 `_mcp_app_event`는 `resourceUri`까지만 실어 보낸다. 반면 릴레이
라우트(`/api/mcp-apps/*`)는 record_id로만 엔드포인트를 해석한다 — 브라우저가 보낸
엔드포인트를 신뢰하지 않는 것이 그 라우트의 존재 이유다.

그 사이를 브라우저가 메우고 있었던 것이 버그였다. `useStream.ts`가 채팅 상대인
**에이전트(A2A) 레코드** id를 넣었고, 릴레이는 그 레코드에 MCP 엔드포인트가 없으니
400을 돌려줬다. 앱은 실측 배포에서 항상 오류 카드로 떴다.

식별 키를 `resourceUri`로 잡은 이유: 툴 이름은 게이트웨이가 타깃 이름을 접두사로
붙여(`platform-status-app___get_platform_status`) 원본 서버의 이름과 달라진다.
`resourceUri`는 원본 서버가 정한 값이라 경로 중간에서 바뀌지 않는다.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamRequest  # noqa: E402
from models.registry import RegistryRecordSummary  # noqa: E402
from services.mcp_apps_service import McpAppsRelay  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

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
    """레지스트리 스텁. MCP 레코드만 돌려준다."""

    def __init__(self, records):
        self._records = records
        self.list_calls = 0

    def list_records(self, descriptor_type=None, status=None, name=None):
        self.list_calls += 1
        return [
            RegistryRecordSummary(
                record_id=rid, name=rid, descriptor_type="MCP", status="APPROVED"
            )
            for rid in self._records
        ]


class ProbeRelay(McpAppsRelay):
    """세션을 열지 않고 툴 목록을 흉내 낸다. 어느 레코드를 뒤졌는지 기록한다."""

    def __init__(self, registry, tools_by_record, endpointless=()):
        super().__init__(registry=registry)
        self._tools_by_record = tools_by_record
        self._endpointless = set(endpointless)
        self.listed = []

    def resolve_endpoint(self, record_id):
        if record_id in self._endpointless:
            raise ValueError(f"MCP record '{record_id}' has no endpoint URL")
        return f"https://mcp.example/{record_id}/mcp"

    def _session_list_tools(self, url):
        record_id = url.split("/")[-2]
        self.listed.append(record_id)
        return self._tools_by_record.get(record_id, [])


def test_finds_the_record_whose_tool_carries_the_resource_uri():
    registry = StubRegistry(["other", "apps"])
    relay = ProbeRelay(
        registry,
        {
            "other": [StubTool("unrelated")],
            "apps": [StubTool("get_platform_status", APP_URI)],
        },
    )

    assert relay.record_for_resource(APP_URI) == "apps"


def test_a_record_without_an_endpoint_is_skipped_not_fatal():
    """엔드포인트 없는 MCP 레코드가 앞에 있어도 탐색이 멈추지 않는다.

    게이트웨이만 등록된 레코드는 `_mcp_url`이 None을 돌려줘 ValueError가 된다. 실제
    레지스트리에 그런 레코드가 있으므로, 하나 때문에 전체 탐색이 죽으면 앱은 영원히
    렌더링되지 않는다.
    """
    registry = StubRegistry(["broken", "apps"])
    relay = ProbeRelay(
        registry,
        {"apps": [StubTool("get_platform_status", APP_URI)]},
        endpointless=["broken"],
    )

    assert relay.record_for_resource(APP_URI) == "apps"


def test_a_failing_endpoint_is_tried_once_then_remembered():
    """탐색은 라이브 SSE 턴에서 돈다. 응답을 끝내지 않는 엔드포인트는 이제
    TimeoutError로 스킵되지만, 앱이 없는 툴은 캐시되지 않아 `app_for_tool`이 툴
    호출마다 다시 순회한다 — 매번 그 상한(실측 10초)을 물면 KB 에이전트가 툴을 부를
    때마다 10초씩 멈춘다. 한 번 실패한 엔드포인트는 프로세스 안에서 기억해 건너뛴다.
    """
    registry = StubRegistry(["broken", "apps"])

    class Counting(ProbeRelay):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.attempts = {}

        def _session_list_tools(self, url):
            record_id = url.split("/")[-2]
            self.attempts[record_id] = self.attempts.get(record_id, 0) + 1
            if record_id == "broken":
                raise TimeoutError()
            return self._tools_by_record.get(record_id, [])

    relay = Counting(registry, {"apps": [StubTool("get_platform_status", APP_URI)]})

    assert relay.record_for_resource(APP_URI) == "apps"
    # A tool with no app never caches, so this walks the records again.
    assert relay.app_for_tool("nonexistent-tool") is None

    assert relay.attempts["broken"] == 1


def test_unknown_resource_resolves_to_nothing():
    registry = StubRegistry(["other"])
    relay = ProbeRelay(registry, {"other": [StubTool("unrelated")]})

    assert relay.record_for_resource(APP_URI) is None


def test_a_resolved_record_is_not_looked_up_twice():
    """탐색은 모든 MCP 레코드에 세션을 연다. 앱 호출마다 그러면 안 된다."""
    registry = StubRegistry(["apps"])
    relay = ProbeRelay(registry, {"apps": [StubTool("get_platform_status", APP_URI)]})

    assert relay.record_for_resource(APP_URI) == "apps"
    assert relay.record_for_resource(APP_URI) == "apps"
    assert relay.listed == ["apps"]


# ── 스트림 보강 ──────────────────────────────────────────────────────────────

VALUES = {"messages": [{"id": "1", "type": "human", "content": "상태 보여줘"}]}


class StubThread:
    def __init__(self):
        self.values = dict(VALUES)
        self.updated_at = ""


class StubRepo:
    def update(self, thread_id, thread):
        return thread


class StubThreadService:
    def __init__(self):
        self.repository = StubRepo()

    def get_or_create_thread(
        self,
        thread_id,
        owner_sub="",
        initial_values=None,
        agent_record_id="",
        agent_name="", **_kwargs):
        return StubThread()

    def get_thread(self, thread_id):
        return StubThread()

    def update_thread_status(self, thread_id, status):
        pass


class ScriptedClient:
    def __init__(self, events):
        self.events = events

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        for event in self.events:
            yield event


class StubResolver:
    def __init__(self, record_id):
        self._record_id = record_id
        self.calls = []

    def record_for_resource(self, uri):
        self.calls.append(uri)
        return self._record_id


def streamed_app_events(events, resolver):
    threads = StubThreadService()
    client = ScriptedClient(events)
    service = StreamingService(
        thread_service=threads, agentcore_client=client, mcp_apps_relay=resolver
    )
    service._get_agent_client = lambda config=None: client

    seen = []

    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", StreamRequest(values=VALUES)
        )
        async for chunk in response.body_iterator:
            if "mcpApp" in chunk:
                seen.append(chunk)

    asyncio.run(scenario())
    return seen


def app_event(**overrides):
    payload = {
        "toolCallId": "tool-1",
        "toolName": "platform-status-app___get_platform_status",
        "resourceUri": APP_URI,
        "messageId": None,
    }
    payload.update(overrides)
    return {"event": {"mcpApp": payload}}


def test_stream_fills_in_the_serving_record_id():
    resolver = StubResolver("apps")

    seen = streamed_app_events([app_event()], resolver)

    assert len(seen) == 1
    assert '"recordId": "apps"' in seen[0]
    assert resolver.calls == [APP_URI]


def test_a_record_id_already_on_the_event_is_left_alone():
    """런타임이 언젠가 record_id를 알게 되면 그쪽이 이긴다 — 탐색은 폴백이다."""
    resolver = StubResolver("apps")

    seen = streamed_app_events([app_event(recordId="from-runtime")], resolver)

    assert '"recordId": "from-runtime"' in seen[0]
    assert resolver.calls == []


def test_an_unresolvable_app_still_reaches_the_browser():
    """앱을 못 찾아도 이벤트를 삼키지 않는다.

    삼키면 UI에는 아무 일도 없었던 것처럼 보인다. 이벤트를 보내면 McpAppView가
    릴레이 오류를 카드에 표시하므로 사용자와 로그 양쪽에 원인이 남는다.
    """
    seen = streamed_app_events([app_event()], StubResolver(None))

    assert len(seen) == 1
    assert "mcpApp" in seen[0]


def test_a_failing_resolver_does_not_break_the_stream():
    """탐색은 네트워크를 탄다. 그것이 대화 전체를 죽이면 안 된다."""

    class Exploding:
        def record_for_resource(self, uri):
            raise RuntimeError("registry down")

    seen = streamed_app_events([app_event()], Exploding())

    assert len(seen) == 1
