"""harness 스트림이 artifact 패널 이벤트를 낸다.

런타임 에이전트는 `create_artifact`/`update_artifact` 호출을 `agent-runtime/main.py`
가 이름으로 가로채, 툴 *입력*에 실린 문서를 `artifact` 이벤트로 스트림에 주입한다.
harness 는 그 코드를 지나지 않는다 — AWS 가 실행하고 서버는 `InvokeHarness` 의
Converse 이벤트만 받으므로, harness 가 게이트웨이의 artifact 툴을 불러도 패널이 열릴
길이 없었다.

그래서 스트리밍 서비스가 툴 이름으로 그 호출을 알아채 `artifact` 이벤트를 합성한다.
`mcpApp` 폴백과 같은 자리·같은 규칙이다: 블록이 끝날 때(`contentBlockStop`), harness
스트림에만. 런타임은 스스로 같은 이벤트를 그 시점에 내므로 양쪽에서 내면 artifact 가
두 번 저장된다.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.harness_client import HarnessClient  # noqa: E402
from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "1", "type": "human", "content": "리포트 만들어줘"}]}


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

    def get_or_create_thread(self, thread_id, owner_sub="", initial_values=None,
                             agent_record_id="", agent_name="", **_kwargs):
        return StubThread()

    def get_thread(self, thread_id):
        return StubThread()

    def update_thread_status(self, thread_id, status):
        pass


class ScriptedHarness(HarnessClient):
    """harness 스트림을 흉내 낸다. 서비스는 클라이언트 종류로 이 경로를 가른다."""

    def __init__(self, events):
        super().__init__(harness_arn="arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1")
        self.events = events

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        for event in self.events:
            yield event


class ScriptedRuntime:
    """런타임 에이전트 스트림. artifact 이벤트를 스스로 낸다."""

    def __init__(self, events):
        self.events = events

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        for event in self.events:
            yield event


def tool_use_start(name, tool_use_id="tool-1", index=1):
    return {
        "event": {
            "contentBlockStart": {
                "contentBlockIndex": index,
                "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}},
            }
        }
    }


def tool_use_input(payload, index=1):
    return {
        "event": {
            "contentBlockDelta": {
                "contentBlockIndex": index,
                "delta": {"toolUse": {"input": json.dumps(payload)}},
            }
        }
    }


def block_stop(index=1):
    return {"event": {"contentBlockStop": {"contentBlockIndex": index}}}


def artifact_call(name, payload, tool_use_id="tool-1"):
    """한 artifact 툴 호출의 전체 블록. 신호는 블록이 끝날 때 나온다."""
    return [tool_use_start(name, tool_use_id), tool_use_input(payload), block_stop()]


def streamed(events, client_factory=ScriptedHarness):
    threads = StubThreadService()
    client = client_factory(events)
    service = StreamingService(thread_service=threads, agentcore_client=client)
    service._get_agent_client = lambda config=None: client

    chunks = []

    async def scenario():
        response = await service.stream_thread_execution("t-1", StreamRequest(values=VALUES))
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    asyncio.run(scenario())
    return chunks


def artifact_chunks(chunks):
    out = []
    for c in chunks:
        if not c.startswith("data: "):
            continue
        payload = json.loads(c[len("data: "):])
        if "artifact" in payload.get("event", {}):
            out.append(payload["event"]["artifact"])
    return out


REPORT = {"artifact_id": "q3", "title": "Q3", "kind": "markdown", "content": "# Q3\nbody"}


def test_a_create_artifact_call_produces_an_artifact_event():
    arts = artifact_chunks(streamed(artifact_call("create_artifact", REPORT)))

    assert len(arts) == 1
    assert arts[0]["artifactId"] == "q3"
    assert arts[0]["title"] == "Q3"
    assert arts[0]["kind"] == "markdown"
    assert arts[0]["content"] == "# Q3\nbody"
    assert arts[0]["toolCallId"] == "tool-1"


def test_a_gateway_prefixed_name_is_recognised():
    """harness 는 MCP 레코드를 게이트웨이 도구로 붙이므로 접두사가 기본값이다."""
    arts = artifact_chunks(streamed(artifact_call("platform-tools___create_artifact", REPORT)))

    assert len(arts) == 1
    assert arts[0]["artifactId"] == "q3"


def test_update_artifact_is_recognised_too():
    payload = {**REPORT, "content": "# Q3\nrevised"}
    arts = artifact_chunks(streamed(artifact_call("platform-tools___update_artifact", payload)))

    assert len(arts) == 1
    assert arts[0]["content"] == "# Q3\nrevised"


def test_an_unknown_kind_falls_back_to_text():
    payload = {**REPORT, "kind": "spreadsheet"}
    arts = artifact_chunks(streamed(artifact_call("create_artifact", payload)))

    assert arts[0]["kind"] == "text"


def test_a_non_artifact_tool_produces_no_artifact_event():
    arts = artifact_chunks(streamed(artifact_call("web_search", {"query": "x"})))

    assert arts == []


def test_a_call_without_content_produces_no_artifact_event():
    payload = {"artifact_id": "q3", "title": "Q3", "kind": "markdown"}
    arts = artifact_chunks(streamed(artifact_call("create_artifact", payload)))

    assert arts == []


def test_the_same_call_is_not_signalled_twice():
    """harness 는 툴 결과 턴의 블록 stop 을 한 번 더 보낸다. 호출 단위로 기억한다."""
    events = artifact_call("create_artifact", REPORT) + [block_stop()]

    assert len(artifact_chunks(streamed(events))) == 1


def test_a_runtime_agents_own_artifact_is_not_doubled():
    """런타임은 스스로 artifact 를 낸다. 그 위에 합성으로 또 얹지 않는다.

    합성은 harness 스트림에만 적용한다 — 런타임의 이벤트는 `contentBlockStop` 과 같은
    시점에 나오므로 클라이언트 종류로 가르는 것만이 타이밍에 의존하지 않는 경계다.
    """
    events = [
        tool_use_start("create_artifact"),
        tool_use_input(REPORT),
        block_stop(),
        {"event": {"artifact": {"toolCallId": "tool-1", "artifactId": "q3",
                                "title": "Q3", "kind": "markdown", "content": "# Q3\nbody"}}},
    ]

    arts = artifact_chunks(streamed(events, client_factory=ScriptedRuntime))

    assert len(arts) == 1
