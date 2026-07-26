from orchestrator.correction import (
    CorrectionState,
    next_attempt,
    should_retry,
    terminal_reason,
)
from orchestrator.mcp_parsing import SqlResult


def _ok():
    return SqlResult(status="ok", columns=["a"], rows=[[1]], row_count=1)


def _rejected():
    return SqlResult(status="rejected", reason="DELETE 금지", rule="SELECT_ONLY")


def _error():
    return SqlResult(status="error", message="syntax error")


def test_no_retry_on_success():
    st = CorrectionState(attempt=1, max_corrections=3, last_result=_ok())
    assert should_retry(st) is False
    assert terminal_reason(st) is None


def test_no_retry_when_no_result():
    st = CorrectionState(attempt=0, max_corrections=3, last_result=None)
    assert should_retry(st) is False
    assert terminal_reason(st) is None


def test_retry_on_rejected_within_budget():
    st = CorrectionState(attempt=1, max_corrections=3, last_result=_rejected())
    assert should_retry(st) is True
    assert terminal_reason(st) is None


def test_retry_on_error_within_budget():
    st = CorrectionState(attempt=2, max_corrections=3, last_result=_error())
    assert should_retry(st) is True


def test_no_retry_when_budget_exhausted_rejected():
    # attempt 4 > max_corrections 3 → 예산 소진
    st = CorrectionState(attempt=4, max_corrections=3, last_result=_rejected())
    assert should_retry(st) is False
    reason = terminal_reason(st)
    assert reason is not None
    assert "안전하게" in reason


def test_no_retry_when_budget_exhausted_error():
    st = CorrectionState(attempt=4, max_corrections=3, last_result=_error())
    assert should_retry(st) is False
    reason = terminal_reason(st)
    assert reason is not None
    assert "4회" in reason  # max_corrections + 1


def test_boundary_last_allowed_attempt():
    # attempt == max_corrections 는 아직 재시도 가능(다음 시도가 예산 내)
    st = CorrectionState(attempt=3, max_corrections=3, last_result=_error())
    assert should_retry(st) is True


def test_next_attempt_increments_and_preserves():
    st = CorrectionState(attempt=1, max_corrections=3, last_result=_error())
    nxt = next_attempt(st)
    assert nxt.attempt == 2
    assert nxt.max_corrections == 3
    assert nxt.last_result is st.last_result


def test_exhausted_property():
    assert CorrectionState(attempt=4, max_corrections=3).exhausted is True
    assert CorrectionState(attempt=3, max_corrections=3).exhausted is False
