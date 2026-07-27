"""self-correction 루프 판단 로직 (순수).

execution 결과에 따라 재시도할지/중단할지, 몇 번째 시도인지 결정한다.
Graph 조건부 엣지의 조건 함수가 이 로직을 그대로 호출한다.
LLM/MCP 의존이 없어 단위 테스트로 완전 커버한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .mcp_parsing import SqlResult


@dataclass(frozen=True)
class CorrectionState:
    """self-correction 루프 상태.

    - attempt: 지금까지 실행한 SQL 시도 횟수 (0 = 아직 실행 전)
    - max_corrections: 재시도(재생성) 허용 횟수. 총 실행 시도 수 = max_corrections + 1
    - last_result: 직전 실행 결과 (없으면 None)
    """

    attempt: int
    max_corrections: int
    last_result: SqlResult | None = None

    @property
    def exhausted(self) -> bool:
        """재시도 예산을 모두 소진했는지. 총 시도 = 최초 1회 + max_corrections 회."""
        return self.attempt > self.max_corrections


def should_retry(state: CorrectionState) -> bool:
    """execution 이후 SQL 을 재생성해야 하는지 판단.

    조건: 직전 결과가 rejected/error 이고, 재시도 예산이 남아 있을 것.
    """
    result = state.last_result
    if result is None or not result.needs_correction:
        return False
    # attempt 는 방금 실행한 시도 번호(1부터). 다음 시도가 예산 내인지 확인.
    return state.attempt <= state.max_corrections


def next_attempt(state: CorrectionState) -> CorrectionState:
    """다음 시도로 상태를 진행."""
    return CorrectionState(
        attempt=state.attempt + 1,
        max_corrections=state.max_corrections,
        last_result=state.last_result,
    )


def terminal_reason(state: CorrectionState) -> str | None:
    """루프를 종료해야 하는 경우 사용자에게 전달할 사유. 계속 진행이면 None."""
    result = state.last_result
    if result is None:
        return None
    if result.ok:
        return None
    if not should_retry(state):
        # 예산 소진 상태에서의 최종 실패 메시지.
        if result.status == "rejected":
            return (
                f"요청을 안전하게 처리할 SQL 을 생성하지 못했습니다. "
                f"마지막 거부 사유: {result.reason or '사유 미상'}"
            )
        return (
            f"SQL 실행에 {state.max_corrections + 1}회 시도했지만 실패했습니다. "
            f"마지막 오류: {result.message or '메시지 없음'}"
        )
    return None
