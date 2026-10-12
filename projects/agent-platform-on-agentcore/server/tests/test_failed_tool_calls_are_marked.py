"""실패한 툴 호출은 실패로 보여야 한다.

harness 는 툴의 성패를 알려준다 — `HarnessToolResultBlockStart.status` 가
`HarnessToolUseStatus`(`success` | `error`) 다. 어댑터는 이걸 버리고 있었고, 받는 쪽은
결과가 도착했다는 사실만으로 `completed` 를 찍는다. 그래서 툴이 터져서 스택트레이스를
결과로 돌려줘도 UI 에는 체크 표시가 뜬다 — 에이전트가 왜 엉뚱한 답을 했는지 화면만
봐서는 알 수 없다.

status 는 delta 가 아니라 **블록 시작**에만 실려 온다. 그러니 어댑터가 시작 시점에
기억해두고 내보내는 모든 `toolResult` 에 다시 찍어야 한다 — 소비자는 결과를 덮어쓰므로
(이 파일 옆의 test_streamed_tool_result_is_whole.py 참고) 마지막 이벤트에 status 가
없으면 앞서 보낸 것이 지워진다.

status 가 아예 없으면 키를 넣지 않는다. 런타임 에이전트의 typed 프레임에는 성패 개념이
없으므로, 없는 것을 `success` 로 채우면 스트림이 하지 않은 말을 대신 하는 셈이 된다.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.harness_event_adapter import HarnessEventAdapter  # noqa: E402
from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "u1", "type": "human", "content": "hi"}]}


# --- 어댑터: harness 의 status 를 플랫폼 이벤트로 옮긴다 ------------------------


def result_start(status=None, tool_use_id="tu-1", index=1):
    start = {"toolUseId": tool_use_id}
    if status is not None:
        start["status"] = status
    return {
        "contentBlockStart": {
            "contentBlockIndex": index,
            "start": {"toolResult": start},
        }
    }


def result_delta(text, index=1):
    return {
        "contentBlockDelta": {
            "contentBlockIndex": index,
            "delta": {"toolResult": [{"text": text}]},
        }
    }


def emitted(adapter, event):
    return [
        e["event"]["toolResult"]
        for e in adapter.adapt(event)
        if "toolResult" in e.get("event", {})
    ]


def tool_result_turn(status=None):
    adapter = HarnessEventAdapter()
    adapter.adapt({"messageStart": {"role": "user"}})
    adapter.adapt(result_start(status))
    return adapter


def test_a_failed_tool_is_reported_as_failed():
    adapter = tool_result_turn("error")

    payload = emitted(adapter, result_delta("Traceback (most recent call last)"))[0]

    assert payload["status"] == "error"


def test_a_successful_tool_is_reported_as_successful():
    adapter = tool_result_turn("success")

    payload = emitted(adapter, result_delta("1048576"))[0]

    assert payload["status"] == "success"


def test_no_status_reported_means_no_status_claimed():
    """harness 가 말하지 않은 것을 어댑터가 만들어내지 않는다."""
    adapter = tool_result_turn()

    payload = emitted(adapter, result_delta("1048576"))[0]

    assert "status" not in payload


def test_the_status_rides_every_delta_not_just_the_first():
    """소비자가 덮어쓰기 때문이다. 마지막 이벤트에 없으면 지워진다."""
    adapter = tool_result_turn("error")

    statuses = [
        emitted(adapter, result_delta(chunk))[0].get("status")
        for chunk in ("Traceback ", "(most recent ", "call last)")
    ]

    assert statuses == ["error", "error", "error"]


def test_a_reused_block_index_does_not_inherit_the_last_status():
    """인덱스는 메시지 안에서만 센다. 실패가 다음 툴로 번지면 안 된다."""
    adapter = tool_result_turn("error")
    adapter.adapt(result_delta("터졌다"))

    adapter.adapt(result_start("success", tool_use_id="tu-2"))
    payload = emitted(adapter, result_delta("됐다"))[0]

    assert (payload["toolUseId"], payload["status"]) == ("tu-2", "success")


def test_an_unknown_status_is_passed_through_untouched():
    """열거형이 늘어나도 어댑터가 판단을 가로채지 않는다."""
    adapter = tool_result_turn("cancelled")

    payload = emitted(adapter, result_delta("중단"))[0]

    assert payload["status"] == "cancelled"


# --- 저장: 다시 열어도 실패한 채로 남는다 --------------------------------------

TOOL_TURN = [
    {"event": {"messageStart": {"id": "m1", "role": "assistant"}}},
    {
        "event": {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"toolUse": {"toolUseId": "tu-1", "name": "calculate"}},
            }
        }
    },
    {
        "event": {
            "contentBlockDelta": {
                "contentBlockIndex": 1,
                "delta": {"toolUse": {"input": '{"expression":"1/0"}'}},
            }
        }
    },
    {"event": {"contentBlockStop": {"contentBlockIndex": 1}}},
]

STOP = {"event": {"messageStop": {"stopReason": "tool_use", "messageId": "m1"}}}


class StubRepo:
    def __init__(self):
        self.messages = []

    def update(self, thread_id, thread):
        self.messages = thread.values.get("messages", [])
        return thread


class StubThread:
    def __init__(self, messages):
        self.values = {"messages": messages}
        self.updated_at = ""


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
        return self._thread()

    def get_thread(self, thread_id):
        return self._thread()

    def _thread(self):
        carried = [dict(m) for m in self.repository.messages] or list(
            VALUES["messages"]
        )
        return StubThread(carried)

    def update_thread_status(self, thread_id, status):
        pass


class ScriptedClient:
    def __init__(self, events):
        self.events = events

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        for event in self.events:
            yield event


def persisted_tool_calls(events):
    threads = StubThreadService()
    client = ScriptedClient(events)
    service = StreamingService(thread_service=threads, agentcore_client=client)
    service._get_agent_client = lambda config=None: client

    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", StreamRequest(values=VALUES)
        )
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())
    return [
        tc
        for m in threads.repository.messages
        for tc in (m.get("tool_calls") or [])
    ]


def test_a_failed_call_is_stored_as_failed():
    """다시 열었을 때 라이브에서 본 실패가 성공으로 바뀌면 안 된다."""
    calls = persisted_tool_calls(
        TOOL_TURN
        + [
            {
                "event": {
                    "toolResult": {
                        "toolUseId": "tu-1",
                        "result": "ZeroDivisionError",
                        "status": "error",
                    }
                }
            },
            STOP,
        ]
    )

    assert [c["status"] for c in calls] == ["error"]
    assert calls[0]["result"] == "ZeroDivisionError"


def test_a_successful_call_is_still_stored_as_completed():
    """`success` 는 UI 어휘로는 `completed` 다. 그대로 흘리면 상태 없는 칸이 된다."""
    calls = persisted_tool_calls(
        TOOL_TURN
        + [
            {
                "event": {
                    "toolResult": {
                        "toolUseId": "tu-1",
                        "result": "1048576",
                        "status": "success",
                    }
                }
            },
            STOP,
        ]
    )

    assert [c["status"] for c in calls] == ["completed"]


def test_a_result_with_no_status_is_stored_as_completed():
    """런타임 경로는 성패를 말하지 않는다. 기존 동작이 유지되어야 한다."""
    calls = persisted_tool_calls(
        TOOL_TURN
        + [
            {"event": {"toolResult": {"toolUseId": "tu-1", "result": "1048576"}}},
            STOP,
        ]
    )

    assert [c["status"] for c in calls] == ["completed"]


def test_a_failure_survives_a_stream_that_never_stopped():
    """중간에 끊긴 턴의 마무리 저장도 같은 상태를 써야 한다.

    `messageStop` 없이 끝나면 별도의 최종 flush 경로가 메시지를 쓴다. 그 경로가
    실패를 `completed` 로 덮으면 라이브에서 본 에러가 사라진다.
    """
    calls = persisted_tool_calls(
        TOOL_TURN
        + [
            {
                "event": {
                    "toolResult": {
                        "toolUseId": "tu-1",
                        "result": "ZeroDivisionError",
                        "status": "error",
                    }
                }
            }
        ]
    )

    assert [c["status"] for c in calls] == ["error"]
