from orchestrator.request import DEFAULT_ACTOR_ID, parse_run_input


def test_parse_basic_run_input():
    payload = {
        "threadId": "thread-abc",
        "runId": "run-1",
        "messages": [
            {"id": "1", "role": "user", "content": "안녕"},
            {"id": "2", "role": "assistant", "content": "네"},
            {"id": "3", "role": "user", "content": "지역별 매출 알려줘"},
        ],
    }
    req = parse_run_input(payload)
    assert req.thread_id == "thread-abc"
    assert req.run_id == "run-1"
    assert req.question == "지역별 매출 알려줘"  # latest user message
    assert req.session_id == "thread-abc"  # defaults to threadId
    assert req.actor_id == DEFAULT_ACTOR_ID


def test_actor_and_session_from_forwarded_props():
    payload = {
        "threadId": "t1",
        "runId": "r1",
        "messages": [{"role": "user", "content": "q"}],
        "forwardedProps": {"actorId": "user-42", "sessionId": "sess-9"},
    }
    req = parse_run_input(payload)
    assert req.actor_id == "user-42"
    assert req.session_id == "sess-9"


def test_actor_from_state_fallback():
    payload = {
        "threadId": "t1",
        "messages": [{"role": "user", "content": "q"}],
        "state": {"actorId": "state-user"},
    }
    req = parse_run_input(payload)
    assert req.actor_id == "state-user"


def test_content_as_parts_array():
    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "부분1"}, {"text": "부분2"}]}
        ]
    }
    req = parse_run_input(payload)
    assert req.question == "부분1 부분2"


def test_missing_ids_get_defaults():
    req = parse_run_input({"messages": [{"role": "user", "content": "q"}]})
    assert req.thread_id == "default-thread"
    assert req.run_id == "default-run"
    assert req.session_id == "default-thread"


def test_no_user_message_gives_empty_question():
    req = parse_run_input({"messages": [{"role": "assistant", "content": "hi"}]})
    assert req.question == ""


def test_snake_case_keys_accepted():
    req = parse_run_input(
        {"thread_id": "T", "run_id": "R", "messages": [{"role": "user", "content": "q"}]}
    )
    assert req.thread_id == "T"
    assert req.run_id == "R"
