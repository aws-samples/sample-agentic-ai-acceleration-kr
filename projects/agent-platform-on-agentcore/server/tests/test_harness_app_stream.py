"""harness 스트림이 앱 마운트 신호를 낸다.

`InvokeHarness` 는 AWS 가 툴을 실행하므로 서버가 받는 것은 Converse 이벤트뿐이다 —
툴 정의도 `_meta` 도 오지 않는다. 런타임 에이전트에서 그 일을 하는
`agent-runtime/main.py:_mcp_app_event` 를 harness 는 지나지 않으므로, harness 로 만든
에이전트에서는 앱이 아예 렌더링되지 않았다.

그래서 스트리밍 서비스가 툴 이름으로 앱을 되찾아 `mcpApp` 을 발행한다. 어댑터가 아니라
여기인 이유: 어댑터는 순수 번역기이고 레지스트리·MCP 세션에 접근할 수단이 없다. 이미
`mcpApp` 에 record_id 를 채우는 코드가 이 자리에 있다.

발행 시점은 블록이 시작될 때(`contentBlockStart` 의 toolUse)다 — 규격의 "UI preloading":
인자가 다 모이기 전에 앱을 띄워 `tool-input-partial` 을 스트리밍할 수 있어야 한다.
harness 스트림에만 적용한다 — 런타임 에이전트는 자체 신호를 내므로 양쪽에서 내면 앱이
두 번 마운트된다.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.harness_client import HarnessClient  # noqa: E402
from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

APP_URI = "ui://platform-status/app.html"
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


class ScriptedHarness(HarnessClient):
    """harness 스트림을 흉내 낸다.

    `HarnessClient` 를 상속하는 이유: 서비스는 클라이언트 종류로 이 경로를 가른다.
    아무 스텁이나 쓰면 런타임 스트림에서도 신호가 나가는지를 검증하지 못한다.
    """

    def __init__(self, events):
        super().__init__(harness_arn="arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1")
        self.events = events

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        for event in self.events:
            yield event


class ScriptedRuntime:
    """런타임 에이전트 스트림. 앱 신호를 스스로 낸다."""

    def __init__(self, events):
        self.events = events

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        for event in self.events:
            yield event


class StubRelay:
    """툴 이름으로 앱을 찾는 릴레이 스텁."""

    def __init__(self, apps):
        self._apps = apps
        self.tool_lookups = []

    def app_for_tool(self, tool_name):
        self.tool_lookups.append(tool_name)
        return self._apps.get(tool_name)

    def record_for_resource(self, uri):
        return None


def tool_use_event(name, tool_use_id="tool-1"):
    """harness 가 보내는 형태 그대로. 어댑터가 이것을 번역해 내보낸다."""
    return {
        "event": {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}},
            }
        }
    }


def block_stop(index=1):
    return {"event": {"contentBlockStop": {"contentBlockIndex": index}}}


def tool_call(name, tool_use_id="tool-1"):
    """한 툴 호출의 전체 블록. 신호는 블록이 시작될 때 나온다."""
    return [tool_use_event(name, tool_use_id), block_stop()]


def streamed(events, relay, client_factory=None):
    threads = StubThreadService()
    client = (client_factory or ScriptedHarness)(events)
    service = StreamingService(
        thread_service=threads, agentcore_client=client, mcp_apps_relay=relay
    )
    service._get_agent_client = lambda config=None: client

    chunks = []

    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", StreamRequest(values=VALUES)
        )
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    asyncio.run(scenario())
    return chunks


def app_chunks(chunks):
    return [c for c in chunks if "mcpApp" in c]


def test_a_tool_with_an_app_gets_a_mount_signal():
    relay = StubRelay({"platform-status-app___get_platform_status": ("rec-1", APP_URI)})

    apps = app_chunks(
        streamed(tool_call("platform-status-app___get_platform_status"), relay)
    )

    assert len(apps) == 1
    assert '"recordId": "rec-1"' in apps[0]
    assert f'"resourceUri": "{APP_URI}"' in apps[0]
    assert '"toolCallId": "tool-1"' in apps[0]


def test_the_signal_follows_the_tool_use_block():
    """앱 신호는 그 툴 호출 뒤에 와야 한다.

    앞서 나가면 UI 가 아직 없는 툴 호출에 앱을 붙이려 하고, `matchesMessage` 가
    tool_call_id 로 짝을 맞추므로 카드가 어느 메시지에도 붙지 않는다.
    """
    relay = StubRelay({"get_platform_status": ("rec-1", APP_URI)})

    chunks = streamed(tool_call("get_platform_status"), relay)
    indexes = [i for i, c in enumerate(chunks) if "contentBlockStart" in c or "mcpApp" in c]
    kinds = ["mcpApp" if "mcpApp" in chunks[i] else "toolUse" for i in indexes]

    assert kinds == ["toolUse", "mcpApp"]


def test_a_tool_without_an_app_gets_no_signal():
    apps = app_chunks(streamed(tool_call("calculate"), StubRelay({})))

    assert apps == []


def test_the_same_tool_call_is_not_signalled_twice():
    """한 툴 호출에 앱은 하나다.

    harness 는 delta 를 여러 번, 그리고 툴 결과 턴의 블록 stop 을 한 번 더 보낸다.
    호출 단위로 기억해두지 않으면 그때마다 카드가 한 장씩 쌓인다.
    """
    events = [
        tool_use_event("get_platform_status"),
        {
            "event": {
                "contentBlockDelta": {
                    "contentBlockIndex": 1,
                    "delta": {"toolUse": {"input": "{}"}},
                }
            }
        },
        block_stop(),
        # harness 는 툴 결과 턴을 별도 블록으로 보내므로 stop 이 한 번 더 온다.
        block_stop(),
    ]
    relay = StubRelay({"get_platform_status": ("rec-1", APP_URI)})

    assert len(app_chunks(streamed(events, relay))) == 1


def test_a_runtime_agents_own_signal_is_not_doubled():
    """런타임 에이전트는 스스로 mcpApp 을 낸다. 그 위에 또 얹지 않는다.

    런타임의 신호는 `contentBlockStop` 과 같은 시점에 나오므로 "이미 신호가 나갔나"
    검사만으로는 순서에 따라 갈린다. 그래서 폴백은 harness 스트림에만 적용한다 —
    이 테스트가 그 경계를 지킨다.
    """
    relay = StubRelay({"get_platform_status": ("rec-1", APP_URI)})
    events = [
        tool_use_event("get_platform_status"),
        block_stop(),
        {
            "event": {
                "mcpApp": {
                    "toolCallId": "tool-1",
                    "toolName": "get_platform_status",
                    "resourceUri": APP_URI,
                    "messageId": None,
                }
            }
        },
    ]

    apps = app_chunks(streamed(events, relay, client_factory=ScriptedRuntime))

    assert len(apps) == 1
    assert relay.tool_lookups == []


def test_a_failing_lookup_does_not_break_the_stream():
    class Exploding:
        def app_for_tool(self, tool_name):
            raise RuntimeError("registry down")

        def record_for_resource(self, uri):
            return None

    chunks = streamed(tool_call("get_platform_status"), Exploding())

    assert any("contentBlockStart" in c for c in chunks)
    assert app_chunks(chunks) == []
