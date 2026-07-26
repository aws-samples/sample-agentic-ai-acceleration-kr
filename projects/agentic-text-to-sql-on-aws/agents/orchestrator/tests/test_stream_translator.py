from orchestrator.ids import SequentialIdFactory
from orchestrator.stream_translator import StreamTranslator


def _translate_all(events):
    tr = StreamTranslator(SequentialIdFactory("r"))
    out = []
    for e in events:
        out.extend(tr.translate(e))
    out.extend(tr.finalize())
    return out


def _types(events):
    return [e["type"] for e in events]


def test_text_deltas_wrapped_in_start_end():
    out = _translate_all([{"data": "안녕"}, {"data": "하세요"}])
    assert _types(out) == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]
    # 같은 메시지 ID 로 묶임
    ids = {e["messageId"] for e in out}
    assert len(ids) == 1
    assert out[1]["delta"] == "안녕"
    assert out[2]["delta"] == "하세요"


def test_tool_use_emits_start_and_args():
    out = _translate_all(
        [{"current_tool_use": {"toolUseId": "t-1", "name": "search_schema", "input": '{"q":"x"}'}}]
    )
    types = _types(out)
    assert types[0] == "TOOL_CALL_START"
    assert out[0]["toolCallName"] == "search_schema"
    assert "TOOL_CALL_ARGS" in types
    assert types[-1] == "TOOL_CALL_END"  # finalize closes it


def test_tool_args_deduplicated():
    # 동일 인자 스냅샷이 반복돼도 ARGS 는 한 번만
    tr = StreamTranslator(SequentialIdFactory("r"))
    out = []
    event = {"current_tool_use": {"toolUseId": "t-1", "name": "run_sql", "input": "SELECT 1"}}
    for _ in range(3):
        out.extend(tr.translate(event))
    args = [e for e in out if e["type"] == "TOOL_CALL_ARGS"]
    assert len(args) == 1


def test_text_after_tool_closes_tool():
    out = _translate_all(
        [
            {"current_tool_use": {"toolUseId": "t-1", "name": "run_sql", "input": "SELECT 1"}},
            {"data": "결과는"},
        ]
    )
    types = _types(out)
    # 도구 종료가 텍스트 시작보다 앞
    assert types.index("TOOL_CALL_END") < types.index("TEXT_MESSAGE_START")


def test_graph_node_events_map_to_steps():
    out = _translate_all(
        [
            {"type": "multiagent_node_start", "node_id": "schema_linking"},
            {"type": "multiagent_node_stream", "event": {"data": "찾는 중"}},
            {"type": "multiagent_node_stop", "node_id": "schema_linking"},
        ]
    )
    types = _types(out)
    assert types[0] == "STEP_STARTED"
    assert out[0]["stepName"] == "schema_linking"
    assert "TEXT_MESSAGE_START" in types
    assert "STEP_FINISHED" in types
    # node_stop 전에 텍스트가 닫혀야 함
    assert types.index("TEXT_MESSAGE_END") < types.index("STEP_FINISHED")


def test_multiagent_result_flushes_text():
    out = _translate_all(
        [
            {"type": "multiagent_node_stream", "event": {"data": "부분 요약"}},
            {"type": "multiagent_result", "result": object()},
        ]
    )
    assert "TEXT_MESSAGE_END" in _types(out)


def test_empty_and_unknown_events_ignored():
    out = _translate_all([{}, {"data": ""}, {"foo": "bar"}])
    assert out == []
