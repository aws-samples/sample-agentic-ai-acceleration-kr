"""이벤트 ID 생성기.

stream_translator 에 주입하는 ID 팩토리. 실행 내에서 단조 증가하는 접두사+카운터 형식
(예: msg-1, tool-2)으로 결정적이며 테스트가 쉽다.
"""

from __future__ import annotations

from itertools import count


class SequentialIdFactory:
    """접두사별 순번 ID 를 발급한다."""

    def __init__(self, run_id: str = "run") -> None:
        self._run_id = run_id
        self._counter = count(1)

    def __call__(self, prefix: str) -> str:
        return f"{prefix}-{self._run_id}-{next(self._counter)}"
