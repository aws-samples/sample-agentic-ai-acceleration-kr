from orchestrator.ids import SequentialIdFactory


def test_sequential_ids_are_unique_and_prefixed():
    f = SequentialIdFactory("run7")
    a = f("msg")
    b = f("msg")
    c = f("tool")
    assert a == "msg-run7-1"
    assert b == "msg-run7-2"
    assert c == "tool-run7-3"
    assert len({a, b, c}) == 3
