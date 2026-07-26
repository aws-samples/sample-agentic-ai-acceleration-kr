from orchestrator import agui_events


def test_run_lifecycle_events():
    assert agui_events.run_started("t", "r") == {
        "type": "RUN_STARTED",
        "threadId": "t",
        "runId": "r",
    }
    assert agui_events.run_finished("t", "r") == {
        "type": "RUN_FINISHED",
        "threadId": "t",
        "runId": "r",
    }


def test_run_error_with_and_without_code():
    assert agui_events.run_error("boom") == {"type": "RUN_ERROR", "message": "boom"}
    assert agui_events.run_error("boom", "X") == {
        "type": "RUN_ERROR",
        "message": "boom",
        "code": "X",
    }


def test_step_events():
    assert agui_events.step_started("schema_linking")["stepName"] == "schema_linking"
    assert agui_events.step_finished("execution")["type"] == "STEP_FINISHED"


def test_text_message_events():
    assert agui_events.text_message_start("m1") == {
        "type": "TEXT_MESSAGE_START",
        "messageId": "m1",
        "role": "assistant",
    }
    assert agui_events.text_message_content("m1", "안녕") == {
        "type": "TEXT_MESSAGE_CONTENT",
        "messageId": "m1",
        "delta": "안녕",
    }
    assert agui_events.text_message_end("m1")["type"] == "TEXT_MESSAGE_END"


def test_tool_call_events():
    start = agui_events.tool_call_start("tc1", "run_sql")
    assert start["toolCallId"] == "tc1"
    assert start["toolCallName"] == "run_sql"
    assert "parentMessageId" not in start

    start2 = agui_events.tool_call_start("tc1", "run_sql", parent_message_id="m1")
    assert start2["parentMessageId"] == "m1"

    assert agui_events.tool_call_args("tc1", '{"sql":')["delta"] == '{"sql":'
    assert agui_events.tool_call_end("tc1")["type"] == "TOOL_CALL_END"

    res = agui_events.tool_call_result("m2", "tc1", "{...}")
    assert res == {
        "type": "TOOL_CALL_RESULT",
        "messageId": "m2",
        "toolCallId": "tc1",
        "content": "{...}",
        "role": "tool",
    }
