"""툴 결과가 여러 delta 로 쪼개져 와도 온전한 값 하나로 도착한다.

`InvokeHarness` 의 툴 결과는 델타다. 서비스 모델이 그렇게 말한다 —
`HarnessContentBlockDelta.toolResult` 는 `HarnessToolResultBlocksDelta`, 즉
`HarnessToolResultBlockDelta` 의 리스트이고 그 `text` 는 "A text tool result delta"
다. 그래서 결과가 길면 한 `contentBlockIndex` 에 delta 가 여러 번 온다.

어댑터는 그 delta 하나하나를 완성된 결과로 보고 `toolResult` 이벤트를 매번 새로
내보냈다. 받는 쪽은 둘 다 덮어쓴다 — `useStream` 은 `result: toolResult` 로,
`streaming_service` 는 `["result"] = ...` 로. 그러니 화면과 저장된 기록에는 마지막
조각만 남는다. 앞이 잘려나간 결과가 그 증상이다.

누적은 어댑터가 한다. 받는 쪽을 append 로 고치는 방법도 있지만 그러면 소비자가
둘(브라우저·저장 루프)이라 양쪽이 같은 규칙을 따라야 하고, 하나라도 어긋나면 결과가
두 번 붙는다. 어댑터는 델타 의미를 아는 유일한 자리이므로 여기서 매 이벤트에 누적된
전체를 실어 보낸다 — 덮어쓰기가 그대로 맞는 동작이 된다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.harness_event_adapter import HarnessEventAdapter  # noqa: E402

TOOL_USE_ID = "tooluse-abc"
INDEX = 3


def result_start(tool_use_id=TOOL_USE_ID, index=INDEX):
    return {
        "contentBlockStart": {
            "contentBlockIndex": index,
            "start": {"toolResult": {"toolUseId": tool_use_id}},
        }
    }


def result_delta(blocks, index=INDEX):
    return {
        "contentBlockDelta": {
            "contentBlockIndex": index,
            "delta": {"toolResult": blocks},
        }
    }


def results(adapter, event):
    """어댑터가 낸 toolResult 페이로드들."""
    return [
        e["event"]["toolResult"]
        for e in adapter.adapt(event)
        if "toolResult" in e.get("event", {})
    ]


def feed(events):
    """툴 결과 턴 하나를 통째로 흘려보내고 마지막 결과 문자열을 준다."""
    adapter = HarnessEventAdapter()
    # 툴 결과는 role="user" 턴으로 온다. 억제되는 턴이지만 결과 자체는 통과해야 한다.
    adapter.adapt({"messageStart": {"role": "user"}})
    last = None
    for event in events:
        for payload in results(adapter, event):
            assert payload["toolUseId"] == TOOL_USE_ID
            last = payload["result"]
    return last


def test_a_chunked_text_result_arrives_whole():
    """세 조각으로 온 텍스트 결과는 이어져야 한다."""
    result = feed(
        [
            result_start(),
            result_delta([{"text": "서울의 "}]),
            result_delta([{"text": "기온은 "}]),
            result_delta([{"text": "23도입니다."}]),
        ]
    )

    assert result == "서울의 기온은 23도입니다."


def test_every_event_carries_the_whole_result_so_far():
    """중간 이벤트도 누적값을 실어야 한다.

    소비자가 덮어쓰기 때문이다. 조각만 실어 보내면 스트림이 끊긴 순간 화면에 남는
    것은 마지막 조각뿐이다.
    """
    adapter = HarnessEventAdapter()
    adapter.adapt({"messageStart": {"role": "user"}})
    adapter.adapt(result_start())

    seen = [
        results(adapter, result_delta([{"text": chunk}]))[0]["result"]
        for chunk in ("ab", "cd", "ef")
    ]

    assert seen == ["ab", "abcd", "abcdef"]


def test_a_json_result_is_serialised_once_not_repeated():
    """`json` delta 는 완성된 값이다. 이어붙이면 안 된다.

    `SensitiveJson` 은 document 타입이라 부분값을 담을 수 없다 — 조각난 JSON 을
    표현할 방법이 없으므로 매 delta 가 그 자체로 온전한 값이고, 나중 것이 앞의 것을
    갈아탄다. 문자열처럼 이어붙이면 `{...}{...}` 같은 파싱 불가한 결과가 된다.
    """
    result = feed(
        [
            result_start(),
            result_delta([{"json": {"temp": 23}}]),
        ]
    )

    assert result == '{"temp": 23}'


def test_two_content_blocks_in_one_result_stay_separate():
    """툴 결과의 content 는 리스트다. 위치별로 누적하고 줄로 나눈다."""
    result = feed(
        [
            result_start(),
            result_delta([{"text": "첫째 "}, {"text": "둘째 "}]),
            result_delta([{"text": "줄"}, {"text": "줄"}]),
        ]
    )

    assert result == "첫째 줄\n둘째 줄"


def test_two_tool_results_do_not_bleed_into_each_other():
    """블록마다 누적은 따로다. 한 턴에 툴 두 개가 끝날 수 있다."""
    adapter = HarnessEventAdapter()
    adapter.adapt({"messageStart": {"role": "user"}})
    adapter.adapt(result_start("tooluse-a", index=1))
    adapter.adapt(result_start("tooluse-b", index=2))

    adapter.adapt(result_delta([{"text": "aa"}], index=1))
    adapter.adapt(result_delta([{"text": "bb"}], index=2))
    a = results(adapter, result_delta([{"text": "aa"}], index=1))[0]
    b = results(adapter, result_delta([{"text": "bb"}], index=2))[0]

    assert (a["toolUseId"], a["result"]) == ("tooluse-a", "aaaa")
    assert (b["toolUseId"], b["result"]) == ("tooluse-b", "bbbb")


def test_a_reused_block_index_starts_a_fresh_result():
    """같은 인덱스가 다음 툴 결과에 다시 쓰이면 누적은 처음부터다.

    harness 는 인덱스를 메시지 안에서만 센다. 이월되면 두 번째 툴의 결과 앞에 첫
    번째 툴의 결과가 붙는다.
    """
    adapter = HarnessEventAdapter()
    adapter.adapt({"messageStart": {"role": "user"}})
    adapter.adapt(result_start("tooluse-a", index=1))
    adapter.adapt(result_delta([{"text": "먼저"}], index=1))

    adapter.adapt(result_start("tooluse-b", index=1))
    second = results(adapter, result_delta([{"text": "나중"}], index=1))[0]

    assert (second["toolUseId"], second["result"]) == ("tooluse-b", "나중")
